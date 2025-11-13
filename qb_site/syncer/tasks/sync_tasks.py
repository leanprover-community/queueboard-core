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
from syncer.services.rate_budget import debounce_repo_schedule


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
    backfill_timeline_pages: int = 0,
) -> Dict[str, Any]:
    """Sync a single PR by (repository id, number) using the GraphQL bundle.

    Behavior
    - Preflight header check to skip unchanged PRs based on GitHub ``updatedAt`` vs DB ``last_synced_at``.
    - Executes the PR bundle (single page in V1) and persists labels, timeline, and CI snapshots.
    - Rate-aware deferral: if a rate-limit/low-budget error occurs during the header or bundle call,
      the task returns a non-error result (``reason=deferred_low_budget``) and schedules itself to
      retry at ``resetAt`` (from the last rateLimit snapshot) plus a small jitter.

    Returns a summary dict with counts and rate limit info. Skips the PR if up-to-date.
    """
    repo = Repository.objects.get(id=repo_id)
    client = GitHubClient()

    def _schedule_defer(reset_at: Optional[str], where: str) -> Dict[str, Any]:
        """Schedule a retry of this PR task at resetAt (+ small jitter) and return a summary.

        Behavior
        - Parses the provided ``reset_at`` (ISO8601) and schedules this task with the same
          parameters at that time plus a small jitter (5s).
        - Returns a non-error summary so the task is not marked as a failure in Celery results.
        """
        eta = None
        if isinstance(reset_at, str):
            try:
                rdt = dtparser.isoparse(reset_at)
                if timezone.is_naive(rdt):
                    rdt = timezone.make_aware(rdt)
                eta = rdt + timedelta(seconds=5)
            except Exception:
                eta = None
        if eta is not None:
            try:
                sync_pr_task.apply_async(
                    args=(repo_id, int(number)),
                    kwargs={
                        "timelineK": timelineK,
                        "commitsM": commitsM,
                        "max_timeline_pages": max_timeline_pages,
                        "max_commit_pages": max_commit_pages,
                        "dry_run": dry_run,
                        "timeline_since_iso": timeline_since_iso,
                    },
                    eta=eta,
                )
            except Exception:
                pass
        rl = client.get_last_rate_limit() or {}
        return {
            "skipped": True,
            "status": "deferred",
            "reason": "deferred_low_budget",
            "where": where,
            "repo": f"{repo.owner}/{repo.name}",
            "repo_id": repo.id,
            "number": int(number),
            "dry_run": dry_run,
            "rate_limit": rl,
            "retry_eta": eta.isoformat() if eta is not None else None,
        }

    # Initialize rate event capture list (include header/bundle/page costs)
    rate_events: list[dict] = []

    # Preflight header to skip unchanged PRs (rate-aware: may defer on low budget error)
    try:
        header = client.get_pr_header(owner=repo.owner, name=repo.name, number=int(number))
    except Exception:
        # If we hit a rate-related error here, defer to resetAt rather than failing the task
        rl0 = client.get_last_rate_limit() or {}
        remaining0 = rl0.get("remaining") if isinstance(rl0, dict) else None
        reset_at0 = rl0.get("resetAt") if isinstance(rl0, dict) else None
        threshold = int(getattr(settings, "SYNCER_RATE_REMAINING_MIN", 200))
        if isinstance(remaining0, int) and remaining0 <= threshold:
            return _schedule_defer(reset_at0, where="header")
        # Unknown error or missing snapshot → surface as failure
        raise
    pr_node = ((header.get("data") or {}).get("repository") or {}).get("pullRequest")

    # Capture header rate snapshot as an event if present (immediately after header call)
    rlh = client.get_last_rate_limit() or {}
    if isinstance(rlh, dict):
        re = {k: rlh.get(k) for k in ("cost", "remaining", "resetAt")}
        re["label"] = "pr_header"
        rate_events.append(re)  # type: ignore[arg-type]

    if pr_node:
        gh_updated = _parse_iso_awareness(pr_node.get("updatedAt"))
    else:
        gh_updated = None

    pr_db = PullRequest.objects.filter(repository=repo, number=int(number)).first()
    if pr_db and pr_db.last_synced_at and gh_updated and gh_updated <= pr_db.last_synced_at:
        # PR unchanged, but we may still spend backfill budget on older timeline pages.
        pages_used = 0
        events_created = 0
        if backfill_timeline_pages and not pr_db.timeline_backfill_done:
            from syncer.services.sub.timeline_sync import sync_timeline_events

            before = pr_db.timeline_backfill_cursor  # may be None to seed
            while pages_used < int(backfill_timeline_pages) and not pr_db.timeline_backfill_done:
                tdata = client.get_timeline_page_back(
                    owner=repo.owner,
                    name=repo.name,
                    number=int(number),
                    last=timelineK,
                    before=before,
                )
                tpr = ((tdata.get("data") or {}).get("repository") or {}).get("pullRequest") or {}
                titems = tpr.get("timelineItems") or {}
                nodes = titems.get("nodes") or []
                tl_res = sync_timeline_events(pr_db, nodes)
                events_created += tl_res.created
                # Update earliest timestamp if present
                if nodes:
                    try:
                        times = [n.get("createdAt") for n in nodes if isinstance(n, dict) and n.get("createdAt")]
                        if times:
                            ts = [dtparser.isoparse(x) for x in times]
                            ts = [timezone.make_aware(t) if timezone.is_naive(t) else t for t in ts]
                            mn = min(ts)
                            if not pr_db.timeline_earliest_synced_at or mn < pr_db.timeline_earliest_synced_at:
                                pr_db.timeline_earliest_synced_at = mn
                    except Exception:  # pragma: no cover
                        pass
                # Update cursor/done flags
                pinfo = titems.get("pageInfo") or {}
                pr_db.timeline_backfill_done = not bool(pinfo.get("hasPreviousPage"))
                before = pinfo.get("startCursor")
                pr_db.timeline_backfill_cursor = before
                pr_db.save(
                    update_fields=[
                        "timeline_backfill_cursor",
                        "timeline_backfill_done",
                        "timeline_earliest_synced_at",
                    ]
                )
                rl_page = client.get_last_rate_limit() or {}
                if isinstance(rl_page, dict):
                    rate_events.append(
                        {
                            "label": "timeline_page_back",
                            "cost": rl_page.get("cost"),
                            "remaining": rl_page.get("remaining"),
                            "resetAt": rl_page.get("resetAt"),
                        }
                    )
                pages_used += 1

        rl = client.get_last_rate_limit() or {}
        status = "backfill_only" if (events_created > 0 or pages_used > 0) else "no_work"
        log.info(
            "sync_pr_task: status=%s repo=%s/%s pr=%s backfill_pages=%s created_events=%s done_after=%s remaining=%s resetAt=%s",
            status,
            repo.owner,
            repo.name,
            number,
            pages_used,
            events_created,
            bool(pr_db.timeline_backfill_done),
            rl.get("remaining"),
            rl.get("resetAt"),
        )
        return {
            "skipped": status == "no_work",
            "status": status,
            "reason": "up_to_date" if status == "no_work" else None,
            "repo": f"{repo.owner}/{repo.name}",
            "repo_id": repo.id,
            "number": int(number),
            "dry_run": dry_run,
            "rate_limit": rl,
            "rate_events": rate_events,
            "backfill": {"pages_used": pages_used, "done_after": bool(pr_db.timeline_backfill_done)},
            "params": {
                "timelineK": timelineK,
                "commitsM": commitsM,
                "max_timeline_pages": max_timeline_pages,
                "max_commit_pages": max_commit_pages,
                "backfill_timeline_pages": backfill_timeline_pages,
            },
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
            # Also capture in summary for metrics aggregation
            rate_events.append(
                {
                    "label": label,
                    "cost": rl_snap.get("cost"),
                    "remaining": rl_snap.get("remaining"),
                    "resetAt": rl_snap.get("resetAt"),
                }
            )
        except Exception:  # pragma: no cover - defensive
            pass

    try:
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
            backfill_timeline_pages=backfill_timeline_pages,
        )
    except Exception:
        # If a rate-related error occurs mid-sync, prefer deferral over failure when snapshot indicates low budget
        rl1 = client.get_last_rate_limit() or {}
        remaining1 = rl1.get("remaining") if isinstance(rl1, dict) else None
        reset_at1 = rl1.get("resetAt") if isinstance(rl1, dict) else None
        threshold = int(getattr(settings, "SYNCER_RATE_REMAINING_MIN", 200))
        if isinstance(remaining1, int) and remaining1 <= threshold:
            return _schedule_defer(reset_at1, where="bundle")
        raise
    rl_final = client.get_last_rate_limit() or {}
    summary: Dict[str, Any] = {
        "skipped": False,
        "status": "synced",
        "repo": f"{repo.owner}/{repo.name}",
        "repo_id": repo.id,
        "number": int(number),
        "dry_run": dry_run,
        "params": {
            "timelineK": timelineK,
            "commitsM": commitsM,
            "max_timeline_pages": max_timeline_pages,
            "max_commit_pages": max_commit_pages,
            "backfill_timeline_pages": backfill_timeline_pages,
        },
        "counts": res,
        "rate_limit": rl_final,
        "rate_events": rate_events,
    }
    # Derive backfill pages used from rate_events labels (if any), and include whether backfill is done now
    try:
        backfill_pages_used = sum(1 for ev in rate_events if isinstance(ev, dict) and ev.get("label") == "timeline_page_back")
    except Exception:
        backfill_pages_used = 0
    # Reload PR to check backfill_done flag after run
    try:
        pr_now = PullRequest.objects.filter(repository=repo, number=int(number)).only("timeline_backfill_done").first()
        backfill_done_now = bool(pr_now.timeline_backfill_done) if pr_now else None
    except Exception:
        backfill_done_now = None
    log.info(
        "sync_pr_task: status=%s repo=%s/%s pr=%s counts=%s backfill_pages=%s done_after=%s remaining=%s resetAt=%s",
        "synced",
        repo.owner,
        repo.name,
        number,
        res,
        backfill_pages_used,
        backfill_done_now,
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

    Behavior
    - Uses a sliding window cutoff (from settings) when ``since_iso`` is not provided.
    - After discovery, reads the last ``rateLimit`` snapshot (captured during the call) and:
      - if ``remaining <= SYNCER_RATE_REMAINING_MIN``: stops early and schedules a continuation
        of this task at ``resetAt`` with a small jitter, debounced via Redis so only one
        continuation is scheduled per repo/resetAt.
      - otherwise enqueues one ``sync_pr_task`` per discovered PR number.
    - Per-repo Postgres advisory lock prevents overlapping runs for the same repository.

    Returns a summary including discovery/enqueue counts, the rate limit snapshot, and a
    ``low_budget`` flag indicating whether a continuation was scheduled.
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

        # Snapshot after discovery
        rl = client.get_last_rate_limit() or {}
        remaining = rl.get("remaining") if isinstance(rl, dict) else None
        reset_at = rl.get("resetAt") if isinstance(rl, dict) else None

        enqueued = 0
        threshold = int(getattr(settings, "SYNCER_RATE_REMAINING_MIN", 200))
        low_budget = isinstance(remaining, int) and remaining <= threshold
        if low_budget:
            # Stop early; schedule a continuation at resetAt + small jitter if possible.
            eta = None
            if isinstance(reset_at, str):
                try:
                    rdt = dtparser.isoparse(reset_at)
                    if timezone.is_naive(rdt):
                        rdt = timezone.make_aware(rdt)
                    # jitter of 5 seconds
                    eta = rdt + timedelta(seconds=5)
                except Exception:
                    eta = None
            if eta is not None and debounce_repo_schedule(repo.id, reset_at):
                # schedule a continuation with same parameters
                try:
                    sync_repo_since_task.apply_async(
                        kwargs={
                            "repo_id": repo.id,
                            "since_iso": cutoff_iso,
                            "limit": lim,
                            "states": st,
                            "timelineK": tk,
                            "commitsM": cm,
                            "dry_run": dry_run,
                        },
                        eta=eta,
                    )
                except Exception:
                    pass
        else:
            # Proceed to enqueue per-PR tasks in batches sized by remaining budget.
            batch_max = int(getattr(settings, "SYNCER_REPO_ENQUEUE_BATCH_MAX", 30))
            est_cost = int(getattr(settings, "SYNCER_EST_COST_PER_PR", 150))
            to_enqueue = len(numbers)
            if isinstance(remaining, int):
                allowed = max(0, remaining - threshold)
                dynamic_cap = allowed // max(1, est_cost)
                if dynamic_cap <= 0:
                    to_enqueue = min(batch_max, len(numbers), 1)
                else:
                    to_enqueue = min(len(numbers), batch_max, int(dynamic_cap))
            else:
                to_enqueue = min(len(numbers), batch_max)

            for num in numbers[:to_enqueue]:
                sync_pr_task.delay(
                    repo.id,
                    int(num),
                    timelineK=tk,
                    commitsM=cm,
                    dry_run=dry_run,
                    backfill_timeline_pages=int(getattr(settings, "SYNCER_TIMELINE_BACKFILL_PAGES", 0)),
                )
                enqueued += 1
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
            "low_budget": bool(low_budget),
            "batch_max": int(getattr(settings, "SYNCER_REPO_ENQUEUE_BATCH_MAX", 30)),
            "discovery_cost": rl.get("cost") if isinstance(rl, dict) else None,
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
