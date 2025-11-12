from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Sequence
from datetime import timedelta

from celery import shared_task
from dateutil import parser as dtparser
from django.utils import timezone
from django.conf import settings

from core.models import Repository
from syncer.models import PullRequest
from syncer.services.github_client import GitHubClient
from syncer.services.pr_sync_service import PRSyncService
from core.utils.locks import repo_advisory_lock


log = logging.getLogger(__name__)


def _parse_iso_awareness(val: Optional[str]) -> Optional[timezone.datetime]:
    if not val:
        return None
    try:
        dt = dtparser.isoparse(val)
    except Exception:  # pragma: no cover - defensive
        return None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    return dt


@shared_task(name="syncer.sync_pr", bind=True)
def sync_pr_task(  # type: ignore[no-redef]
    self,
    repo_id: int,
    number: int,
    *,
    timelineK: int = 150,
    commitsM: int = 15,
    max_timeline_pages: int = 0,
    max_commit_pages: int = 0,
    dry_run: bool = False,
    timeline_since_iso: Optional[str] = None,
) -> Dict[str, Any]:
    """Sync a single PR by (repository id, number) using the GraphQL bundle.

    Returns a summary dict with counts and rate limit info. Skips the PR if
    GitHub's updatedAt is not newer than PullRequest.last_synced_at.
    """
    repo = Repository.objects.get(id=repo_id)
    client = GitHubClient()

    # Preflight header to skip unchanged PRs
    header = client.get_pr_header(owner=repo.owner, name=repo.name, number=int(number))
    pr_node = ((header.get("data") or {}).get("repository") or {}).get("pullRequest")
    if pr_node:
        gh_updated = _parse_iso_awareness(pr_node.get("updatedAt"))
    else:
        gh_updated = None

    pr_db = PullRequest.objects.filter(repository=repo, number=int(number)).first()
    if pr_db and pr_db.last_synced_at and gh_updated and gh_updated <= pr_db.last_synced_at:
        rl = client.get_last_rate_limit() or {}
        log.info(
            "sync_pr_task: up-to-date; skipping repo=%s/%s pr=%s remaining=%s resetAt=%s",
            repo.owner,
            repo.name,
            number,
            rl.get("remaining"),
            rl.get("resetAt"),
        )
        return {
            "skipped": True,
            "reason": "up_to_date",
            "repo": f"{repo.owner}/{repo.name}",
            "repo_id": repo.id,
            "number": int(number),
            "dry_run": dry_run,
            "rate_limit": rl,
        }

    svc = PRSyncService()

    def rate_log(label: str, rl_snap: dict) -> None:
        try:
            log.info(
                "sync_pr_task: rateLimit query=%s cost=%s remaining=%s resetAt=%s",
                label,
                rl_snap.get("cost"),
                rl_snap.get("remaining"),
                rl_snap.get("resetAt"),
            )
        except Exception:  # pragma: no cover - defensive
            pass

    res = svc.sync_pull_request(
        repo,
        number=int(number),
        client=client,
        timelineK=timelineK,
        commitsM=commitsM,
        max_timeline_pages=max_timeline_pages,
        max_commit_pages=max_commit_pages,
        dry_run=dry_run,
        rate_log=rate_log,
        timeline_since_iso_override=timeline_since_iso,
    )
    rl_final = client.get_last_rate_limit() or {}
    summary: Dict[str, Any] = {
        "skipped": False,
        "repo": f"{repo.owner}/{repo.name}",
        "repo_id": repo.id,
        "number": int(number),
        "dry_run": dry_run,
        "params": {
            "timelineK": timelineK,
            "commitsM": commitsM,
            "max_timeline_pages": max_timeline_pages,
            "max_commit_pages": max_commit_pages,
        },
        "counts": res,
        "rate_limit": rl_final,
    }
    log.info(
        "sync_pr_task: done repo=%s/%s pr=%s counts=%s remaining=%s resetAt=%s",
        repo.owner,
        repo.name,
        number,
        res,
        rl_final.get("remaining"),
        rl_final.get("resetAt"),
    )
    return summary


@shared_task(name="syncer.sync_repo_since", bind=True)
def sync_repo_since_task(  # type: ignore[no-redef]
    self,
    repo_id: int,
    *,
    since_iso: Optional[str] = None,
    limit: Optional[int] = None,
    states: Optional[Sequence[str]] = None,
    timelineK: Optional[int] = None,
    commitsM: Optional[int] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Discover changed PRs since cutoff and enqueue per-PR sync tasks.

    V1: simple implementation using a sliding window cutoff when since_iso is not provided.
    Returns summary with counts and last seen rate limit snapshot.
    """
    repo = Repository.objects.get(id=int(repo_id))

    with repo_advisory_lock(repo.id) as acquired:
        if not acquired:
            log.info("sync_repo_since: lock not acquired; skipping repo=%s/%s", repo.owner, repo.name)
            return {"skipped": True, "reason": "lock_not_acquired"}

        client = GitHubClient()
        # Determine cutoff
        if since_iso:
            cutoff_iso = since_iso
        else:
            lookback_min = int(getattr(settings, "SYNCER_DISCOVERY_LOOKBACK_MINUTES", 60))
            cutoff_dt = timezone.now() - timedelta(minutes=lookback_min)
            if timezone.is_naive(cutoff_dt):
                cutoff_dt = timezone.make_aware(cutoff_dt)
            cutoff_iso = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        # Parameters
        lim = int(limit) if isinstance(limit, int) else int(getattr(settings, "SYNCER_DISCOVERY_LIMIT", 30))
        st: list[str]
        if states is None:
            st = [s for s in getattr(settings, "SYNCER_DISCOVERY_STATES_DEFAULT", ["OPEN"]) if s]
        else:
            st = [str(s).upper() for s in states]
        tk = int(timelineK) if isinstance(timelineK, int) else int(getattr(settings, "SYNCER_TIMELINE_K_DEFAULT", 150))
        cm = int(commitsM) if isinstance(commitsM, int) else int(getattr(settings, "SYNCER_COMMITS_M_DEFAULT", 15))

        numbers = client.get_changed_pr_numbers(owner=repo.owner, name=repo.name, since_iso=cutoff_iso, states=st, limit=lim)
        enqueued = 0
        for num in numbers:
            sync_pr_task.delay(repo.id, int(num), timelineK=tk, commitsM=cm, dry_run=dry_run)
            enqueued += 1

        rl = client.get_last_rate_limit() or {}
        log.info(
            "sync_repo_since: repo=%s/%s since=%s discovered=%s enqueued=%s remaining=%s resetAt=%s",
            repo.owner,
            repo.name,
            cutoff_iso,
            len(numbers),
            enqueued,
            rl.get("remaining"),
            rl.get("resetAt"),
        )
        return {
            "skipped": False,
            "repo": f"{repo.owner}/{repo.name}",
            "since": cutoff_iso,
            "discovered": len(numbers),
            "enqueued": enqueued,
            "rate_limit": rl,
        }


@shared_task(name="syncer.sync_active_repos")
def sync_active_repos_task() -> Dict[str, Any]:
    """Enumerate active repositories and enqueue repo-level sync tasks.

    Returns a summary with count of repos considered and tasks enqueued.
    """
    repos = list(Repository.objects.filter(is_active=True).only("id", "owner", "name"))
    enqueued = 0
    for r in repos:
        sync_repo_since_task.delay(r.id)
        enqueued += 1
    return {"repos": len(repos), "enqueued": enqueued}
