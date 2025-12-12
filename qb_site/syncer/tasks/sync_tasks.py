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
from syncer.services.ci_by_sha_service import sync_ci_for_sha
from syncer.models import CheckRun, StatusContext
from core.celery_signals import enqueue_with_parent


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
    backfill_commit_pages: int = 0,
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
                sig = sync_pr_task.s(
                    repo_id,
                    int(number),
                    timelineK=timelineK,
                    commitsM=commitsM,
                    max_timeline_pages=max_timeline_pages,
                    max_commit_pages=max_commit_pages,
                    dry_run=dry_run,
                    timeline_since_iso=timeline_since_iso,
                    backfill_commit_pages=backfill_commit_pages,
                )
                enqueue_with_parent(sig, self.request, eta=eta)
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
    needs_engagement = bool(pr_db and pr_db.engagement_synced_at is None)
    # Ensure we fill head rollup state even if updatedAt hasn’t changed.
    needs_head_ci = bool(pr_db and pr_db.head_ci_state is None)
    # Even when updatedAt is unchanged, we may need to sync to fill engagement/head CI rollup or backfill history.
    if (
        pr_db
        and pr_db.last_synced_at
        and gh_updated
        and gh_updated <= pr_db.last_synced_at
        and not needs_engagement
        and not needs_head_ci
    ):
        # PR unchanged, but we may still spend backfill budget on older timeline pages.
        pages_used = 0
        events_created = 0
        commit_pages_used = 0
        checkruns_upserted = 0
        statusctx_upserted = 0
        # Rate guard: skip backfill when remaining budget is low
        try:
            rl_now = client.get_last_rate_limit() or {}
            remaining_now = rl_now.get("remaining") if isinstance(rl_now, dict) else None
            threshold_now = int(getattr(settings, "SYNCER_RATE_REMAINING_MIN", 200))
            if isinstance(remaining_now, int) and remaining_now <= threshold_now:
                rl = rl_now
                log.info(
                    "sync_pr_task: status=no_work (guarded) repo=%s/%s pr=%s remaining=%s resetAt=%s",
                    repo.owner,
                    repo.name,
                    number,
                    rl.get("remaining"),
                    rl.get("resetAt"),
                )
                return {
                    "skipped": True,
                    "status": "no_work",
                    "reason": "up_to_date",
                    "repo": f"{repo.owner}/{repo.name}",
                    "repo_id": repo.id,
                    "number": int(number),
                    "dry_run": dry_run,
                    "rate_limit": rl,
                    "rate_events": rate_events,
                    "backfill": {
                        "pages_used": 0,
                        "commit_pages_used": 0,
                        "done_after": bool(pr_db.timeline_backfill_done),
                        "commits_done_after": bool(pr_db.commits_backfill_done),
                    },
                    "params": {
                        "timelineK": timelineK,
                        "commitsM": commitsM,
                        "max_timeline_pages": max_timeline_pages,
                        "max_commit_pages": max_commit_pages,
                        "backfill_timeline_pages": backfill_timeline_pages,
                        "backfill_commit_pages": backfill_commit_pages,
                    },
                }
        except Exception:
            pass
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

        # Commit paging backfill: walk the commits connection backward by a small budget
        if backfill_commit_pages and int(backfill_commit_pages) > 0 and not pr_db.commits_backfill_done:
            from syncer.services.sub.ci_sync import sync_check_runs, sync_status_contexts

            # Continue from the saved backfill cursor when available; this ensures
            # successive up-to-date runs progress older pages instead of refetching
            # the newest page each time.
            before: Optional[str] = pr_db.commits_backfill_cursor
            used = 0
            has_prev: Optional[bool] = True
            while used < int(backfill_commit_pages) and has_prev:
                cdata = client.get_commits_page(
                    owner=repo.owner,
                    name=repo.name,
                    number=int(number),
                    last=int(commitsM),
                    before=before,
                )
                cpr = ((cdata.get("data") or {}).get("repository") or {}).get("pullRequest") or {}
                commits = cpr.get("commits") or {}
                nodes = commits.get("nodes") or []
                earliest_candidates: list[str] = []
                for cnode in nodes:
                    commit = (cnode or {}).get("commit") or {}
                    sha = commit.get("oid") or ""
                    contexts = ((commit.get("statusCheckRollup") or {}).get("contexts") or {}).get("nodes") or []
                    cr_contexts = [c for c in contexts if isinstance(c, dict) and c.get("__typename") == "CheckRun"]
                    sc_contexts = [c for c in contexts if isinstance(c, dict) and c.get("__typename") == "StatusContext"]
                    cr_res = sync_check_runs(pr_db, cr_contexts, sha)
                    sc_res = sync_status_contexts(pr_db, sc_contexts, sha)
                    checkruns_upserted += cr_res.created + cr_res.updated
                    statusctx_upserted += sc_res.created + sc_res.updated
                    for ctx in cr_contexts:
                        if isinstance(ctx, dict):
                            if ctx.get("completedAt"):
                                earliest_candidates.append(ctx.get("completedAt"))
                            elif ctx.get("startedAt"):
                                earliest_candidates.append(ctx.get("startedAt"))
                    for ctx in sc_contexts:
                        if isinstance(ctx, dict) and ctx.get("createdAt"):
                            earliest_candidates.append(ctx.get("createdAt"))
                # rate log snapshot for this commit page
                rl_page = client.get_last_rate_limit() or {}
                if isinstance(rl_page, dict):
                    rate_events.append(
                        {
                            "label": "commits_page",
                            "cost": rl_page.get("cost"),
                            "remaining": rl_page.get("remaining"),
                            "resetAt": rl_page.get("resetAt"),
                        }
                    )
                pinfo = commits.get("pageInfo") or {}
                has_prev = bool(pinfo.get("hasPreviousPage"))
                before = pinfo.get("startCursor")
                # Update commit backfill flags on the PR for admin visibility and earliest timestamp (monotone done flag)
                try:
                    pr_db.commits_backfill_cursor = before
                    if not has_prev and not pr_db.commits_backfill_done:
                        pr_db.commits_backfill_done = True
                    if earliest_candidates:
                        from dateutil import parser as _dtp

                        ts = [_dtp.isoparse(x) for x in earliest_candidates if x]
                        ts = [timezone.make_aware(t) if timezone.is_naive(t) else t for t in ts]
                        mn = min(ts) if ts else None
                        if mn is not None and (pr_db.commits_earliest_synced_at is None or mn < pr_db.commits_earliest_synced_at):
                            pr_db.commits_earliest_synced_at = mn
                    pr_db.save(
                        update_fields=[
                            "commits_backfill_cursor",
                            "commits_backfill_done",
                            "commits_earliest_synced_at",
                        ]
                    )
                except Exception:
                    pass
                used += 1
                commit_pages_used += 1

        rl = client.get_last_rate_limit() or {}
        status = "backfill_only" if (events_created > 0 or pages_used > 0 or commit_pages_used > 0) else "no_work"
        log.info(
            "sync_pr_task: status=%s repo=%s/%s pr=%s backfill_pages=%s created_events=%s commit_pages=%s remaining=%s resetAt=%s",
            status,
            repo.owner,
            repo.name,
            number,
            pages_used,
            events_created,
            commit_pages_used,
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
            "backfill": {
                "pages_used": pages_used,
                "commit_pages_used": commit_pages_used,
                "done_after": bool(pr_db.timeline_backfill_done),
                "commits_done_after": bool(pr_db.commits_backfill_done),
            },
            "params": {
                "timelineK": timelineK,
                "commitsM": commitsM,
                "max_timeline_pages": max_timeline_pages,
                "max_commit_pages": max_commit_pages,
                "backfill_timeline_pages": backfill_timeline_pages,
                "backfill_commit_pages": backfill_commit_pages,
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
            backfill_commit_pages=backfill_commit_pages,
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
            "backfill_commit_pages": backfill_commit_pages,
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
    # Kick Analyzer follow-up processing for this PR (best-effort).
    try:
        from analyzer.tasks import process_pr_task

        # Look up the PR id once to pass to the Analyzer task.
        pr_obj = PullRequest.objects.filter(repository=repo, number=int(number)).only("id").first()
        if pr_obj is not None:
            enqueue_with_parent(process_pr_task.s(int(pr_obj.id)), self.request)
    except Exception:
        # Analyzer is best-effort; do not fail the Syncer task if follow-up cannot be scheduled.
        log.exception("sync_pr_task: failed to enqueue analyzer.process_pr for repo=%s/%s pr=%s", repo.owner, repo.name, number)

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
        rate_events: list[dict] = []

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
        if isinstance(rl, dict):
            try:
                rate_events.append(
                    {
                        "label": "repo_discovery",
                        "cost": rl.get("cost"),
                        "remaining": rl.get("remaining"),
                        "resetAt": rl.get("resetAt"),
                    }
                )
            except Exception:
                pass

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
                    sig = sync_repo_since_task.s(
                        repo.id,
                        since_iso=cutoff_iso,
                        limit=lim,
                        states=st,
                        timelineK=tk,
                        commitsM=cm,
                        dry_run=dry_run,
                    )
                    enqueue_with_parent(sig, self.request, eta=eta)
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
                enqueue_with_parent(
                    sync_pr_task.s(
                        repo.id,
                        int(num),
                        timelineK=tk,
                        commitsM=cm,
                        dry_run=dry_run,
                        backfill_timeline_pages=int(getattr(settings, "SYNCER_TIMELINE_BACKFILL_PAGES", 1)),
                        backfill_commit_pages=int(getattr(settings, "SYNCER_COMMITS_BACKFILL_PAGES", 1)),
                    ),
                    self.request,
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
            "rate_events": rate_events,
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


@shared_task(name="syncer.sync_ci_for_shas", bind=True)
def sync_ci_for_shas_task(  # type: ignore[no-redef]
    self,
    repo_id: int,
    number: int,
    *,
    shas: Sequence[str],
    max_pages_per_sha: Optional[int] = None,
    dry_run: bool = False,
    require_pr_association: Optional[bool] = None,
) -> Dict[str, Any]:
    """Fetch CI for a list of commit SHAs for a PR.

    Behavior
    - Respects the global rate budget; if remaining <= SYNCER_RATE_REMAINING_MIN, defers to resetAt.
    - Processes SHAs in order with a per-SHA page cap; stops early on low budget and schedules a continuation for remaining SHAs.
    - Returns a summary with counts and rate snapshots.
    """
    repo = Repository.objects.get(id=int(repo_id))
    pr = PullRequest.objects.get(repository=repo, number=int(number))
    client = GitHubClient()

    rate_events: list[dict] = []

    def rate_log(label: str, rl_snap: dict) -> None:
        try:
            rate_events.append(
                {
                    "label": label,
                    "cost": rl_snap.get("cost"),
                    "remaining": rl_snap.get("remaining"),
                    "resetAt": rl_snap.get("resetAt"),
                }
            )
        except Exception:
            pass

    def _defer(reset_at: Optional[str], remaining_shas: Sequence[str]) -> Dict[str, Any]:
        eta = None
        if isinstance(reset_at, str):
            try:
                rdt = dtparser.isoparse(reset_at)
                if timezone.is_naive(rdt):
                    rdt = timezone.make_aware(rdt)
                eta = rdt + timedelta(seconds=5)
            except Exception:
                eta = None
        if eta is not None and remaining_shas:
            try:
                sig = sync_ci_for_shas_task.s(
                    repo.id,
                    int(number),
                    shas=list(remaining_shas),
                    max_pages_per_sha=max_pages_per_sha,
                    dry_run=dry_run,
                )
                enqueue_with_parent(sig, self.request, eta=eta)
            except Exception:
                pass
        rl = client.get_last_rate_limit() or {}
        return {
            "status": "deferred",
            "repo": f"{repo.owner}/{repo.name}",
            "number": int(number),
            "remaining_shas": list(remaining_shas),
            "rate_limit": rl,
            "rate_events": rate_events,
        }

    # Guard on budget before starting
    rl0 = client.get_rate_limit() or {}
    remaining0 = rl0.get("remaining") if isinstance(rl0, dict) else None
    reset0 = rl0.get("resetAt") if isinstance(rl0, dict) else None
    threshold = int(getattr(settings, "SYNCER_RATE_REMAINING_MIN", 200))
    if isinstance(remaining0, int) and remaining0 <= threshold:
        return _defer(reset0, shas)

    max_pages = (
        int(max_pages_per_sha) if isinstance(max_pages_per_sha, int) else int(getattr(settings, "SYNCER_CI_BY_SHA_PAGES", 1))
    )
    require_assoc = bool(require_pr_association) if require_pr_association is not None else False

    done: list[str] = []
    todo: list[str] = [s for s in shas if s]
    total_counts = {"checkruns_created": 0, "checkruns_updated": 0, "status_created": 0, "status_updated": 0}

    for sha in todo:
        # Check budget before each SHA
        rl_now = client.get_last_rate_limit() or {}
        remaining_now = rl_now.get("remaining") if isinstance(rl_now, dict) else None
        reset_at = rl_now.get("resetAt") if isinstance(rl_now, dict) else None
        if isinstance(remaining_now, int) and remaining_now <= threshold:
            remaining = [s for s in todo if s not in done]
            return _defer(reset_at, remaining)

        if dry_run:
            done.append(sha)
            continue

        res = sync_ci_for_sha(
            pr,
            sha,
            client=client,
            max_pages=max_pages,
            rate_log=rate_log,
            require_pr_association=require_assoc,
        )
        for k in total_counts.keys():
            total_counts[k] += int(res.get(k, 0))
        done.append(sha)

    rl_final = client.get_last_rate_limit() or {}
    return {
        "status": "ok",
        "repo": f"{repo.owner}/{repo.name}",
        "number": int(number),
        "shas_done": done,
        "counts": total_counts,
        "rate_limit": rl_final,
        "rate_events": rate_events,
    }


@shared_task(name="syncer.refresh_pending_ci_for_repo")
def refresh_pending_ci_for_repo_task(  # type: ignore[no-redef]
    repo_id: int,
    *,
    max_prs: int = 20,
    max_shas_per_pr: int = 5,
    max_pending_hours: int | None = None,
) -> Dict[str, Any]:
    """Refresh CI for SHAs whose CheckRuns/StatusContexts are stuck pending.

    Selection
    - Consider PRs in the given repo that currently have any non-terminal CI:
      - CheckRun.status != COMPLETED
      - or StatusContext.state == PENDING
    - For each such PR, collect head_shas for "eligible" pending CI rows:
      - If last_synced_at is NULL: always eligible (never refreshed explicitly).
      - Else, compute how long GitHub has been reporting this row as pending:
        pending_duration = last_synced_at - origin
        where origin is:
          - CheckRun: gh_started_at or gh_completed_at or created_at
          - StatusContext: gh_created_at
        Only include rows where pending_duration < max_pending_hours.
    - From those rows, take up to `max_shas_per_pr` distinct SHAs and enqueue a
      `sync_ci_for_shas_task` to refresh CI for that PR.

    Returns a summary dict with counts and per-PR task ids.
    """
    repo = Repository.objects.get(id=int(repo_id))
    from django.db.models import Exists, OuterRef, Q

    max_prs_int = int(max_prs)
    max_shas_int = int(max_shas_per_pr)
    if max_prs_int <= 0 or max_shas_int <= 0:
        return {
            "repo": f"{repo.owner}/{repo.name}",
            "repo_id": repo.id,
            "prs_considered": 0,
            "prs_enqueued": 0,
            "shas_enqueued": 0,
            "max_prs": max_prs_int,
            "max_shas_per_pr": max_shas_int,
        }

    # Identify PRs that currently have any pending CI.
    pending_cr = CheckRun.objects.filter(pull_request=OuterRef("pk")).exclude(status="COMPLETED")
    pending_sc = StatusContext.objects.filter(pull_request=OuterRef("pk"), state="PENDING")

    prs_qs = (
        PullRequest.objects.filter(repository=repo)
        .annotate(
            has_pending_ci=Exists(pending_cr) | Exists(pending_sc),
        )
        .filter(has_pending_ci=True)
        .order_by("gh_updated_at", "id")
    )

    from django.utils import timezone
    from datetime import timedelta

    # Max age GitHub is allowed to report a CI row as pending before we stop polling it.
    if max_pending_hours is None:
        max_pending_hours = int(getattr(settings, "SYNCER_PENDING_CI_MAX_AGE_HOURS", 48))
    max_age = timedelta(hours=max_pending_hours)

    total_pending_prs = prs_qs.count()
    prs = list(prs_qs[:max_prs_int])
    prs_enqueued = 0
    shas_enqueued = 0
    per_pr: list[dict[str, Any]] = []

    now = timezone.now()

    for pr in prs:
        # Pending CheckRuns with acceptable "pending duration".
        cr_qs = CheckRun.objects.filter(pull_request=pr).exclude(status="COMPLETED")
        eligible_cr_shas: set[str] = set()
        for cr in cr_qs:
            origin = cr.gh_started_at or cr.gh_completed_at or cr.created_at
            if origin is None:
                origin = now
            if cr.last_synced_at is None or (cr.last_synced_at - origin) < max_age:
                if cr.head_sha:
                    eligible_cr_shas.add(cr.head_sha)

        # Pending StatusContexts with acceptable "pending duration".
        sc_qs = StatusContext.objects.filter(pull_request=pr, state="PENDING")
        eligible_sc_shas: set[str] = set()
        for sc in sc_qs:
            origin_sc = sc.gh_created_at or sc.created_at
            if origin_sc is None:
                origin_sc = now
            if sc.last_synced_at is None or (sc.last_synced_at - origin_sc) < max_age:
                if sc.head_sha:
                    eligible_sc_shas.add(sc.head_sha)

        shas = list(eligible_cr_shas | eligible_sc_shas)
        if not shas:
            continue
        shas = shas[:max_shas_int]

        # Enqueue CI refresh for these SHAs.
        pages_per_sha = int(getattr(settings, "SYNCER_CI_BY_SHA_PAGES", 1))
        async_res = sync_ci_for_shas_task.delay(
            repo_id=repo.id,
            number=int(pr.number),
            shas=shas,
            max_pages_per_sha=pages_per_sha,
            dry_run=False,
            require_pr_association=False,
        )
        prs_enqueued += 1
        shas_enqueued += len(shas)
        per_pr.append(
            {
                "number": int(pr.number),
                "shas": shas,
                "task_id": async_res.id,
            }
        )

    return {
        "repo": f"{repo.owner}/{repo.name}",
        "repo_id": repo.id,
        "prs_considered": len(prs),
        "prs_enqueued": prs_enqueued,
        "shas_enqueued": shas_enqueued,
        "backlog_prs": total_pending_prs,
        "max_prs": max_prs_int,
        "max_shas_per_pr": max_shas_int,
        "max_pending_hours": int(max_pending_hours),
        "items": per_pr,
    }


@shared_task(name="syncer.refresh_pending_ci_for_active_repos")
def refresh_pending_ci_for_active_repos_task(  # type: ignore[no-redef]
    max_prs_per_repo: int = 5,
    max_shas_per_pr: int = 3,
    max_pending_hours: int | None = None,
) -> Dict[str, Any]:
    """Enqueue pending-CI refresh for all active repositories."""
    repos = list(Repository.objects.filter(is_active=True).only("id", "owner", "name"))
    enqueued = 0
    for repo in repos:
        refresh_pending_ci_for_repo_task.delay(
            repo.id,
            max_prs=max_prs_per_repo,
            max_shas_per_pr=max_shas_per_pr,
            max_pending_hours=max_pending_hours,
        )
        enqueued += 1
    return {"repos": len(repos), "enqueued": enqueued}
