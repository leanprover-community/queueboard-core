from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from celery import shared_task
from dateutil import parser as dtparser
from django.utils import timezone

from core.models import Repository
from syncer.models import PullRequest
from syncer.services.github_client import GitHubClient
from syncer.services.pr_sync_service import PRSyncService


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
        return {"skipped": True, "reason": "up_to_date", "rate_limit": rl}

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
    )
    rl_final = client.get_last_rate_limit() or {}
    summary: Dict[str, Any] = {
        "skipped": False,
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
