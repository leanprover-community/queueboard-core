from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Sequence
from datetime import datetime, timedelta

from celery import shared_task
from django.db import models
from django.db.models.functions import Coalesce
from dateutil import parser as dtparser
from django.utils import timezone
from django.conf import settings

from core.models import Repository
from syncer.models import PullRequest, RepoDiscoveryState
from syncer.services.github_client import GitHubClient
from syncer.services.pr_sync_service import PRSyncService
from core.utils.locks import repo_advisory_lock
from syncer.services.rate_budget import debounce_repo_schedule
from syncer.services.ci_by_sha_service import sync_ci_for_sha
from syncer.services.ci_backoff import record_ci_sha_fetch, should_enqueue_ci_sha_with_state
from syncer.models import CIShaFetchState, CheckRun, CommitCheckRun, CommitStatusContext, StatusContext
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
    force: bool = False,
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
    client = GitHubClient(operation="syncer_pr_read", owner=repo.owner, repo=repo.name)

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
                    force=force,
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

    header_state = str(pr_node.get("state", "")).lower() if pr_node else None
    header_is_draft = bool(pr_node.get("isDraft")) if pr_node is not None else None

    pr_db = PullRequest.objects.filter(repository=repo, number=int(number)).first()
    state_mismatch = bool(pr_db and header_state and pr_db.state != header_state)
    draft_mismatch = bool(pr_db and header_is_draft is not None and pr_db.is_draft != header_is_draft)
    needs_state_refresh = state_mismatch or draft_mismatch
    needs_engagement = bool(pr_db and pr_db.engagement_synced_at is None)
    # Ensure we fill head rollup state even if updatedAt hasn’t changed.
    pending_head_states = {"PENDING", "EXPECTED", "IN_PROGRESS", "QUEUED"}
    needs_head_ci = bool(pr_db and (pr_db.head_ci_state is None or str(pr_db.head_ci_state).upper() in pending_head_states))
    # Ensure we fill head SHA even if updatedAt hasn’t changed.
    needs_head_sha = bool(pr_db and not (pr_db.head_sha or "").strip())
    last_synced_cutoff = None
    if pr_db and pr_db.last_synced_at:
        eps = int(getattr(settings, "SYNCER_LAST_SYNC_EPSILON_SECONDS", 2))
        last_synced_cutoff = pr_db.last_synced_at - timedelta(seconds=max(0, eps))
        if timezone.is_naive(last_synced_cutoff):
            last_synced_cutoff = timezone.make_aware(last_synced_cutoff)
    # Even when updatedAt is unchanged, we may need to sync to fill engagement/head CI rollup or backfill history.
    if (
        pr_db
        and last_synced_cutoff
        and gh_updated
        and gh_updated <= last_synced_cutoff
        and not needs_state_refresh
        and not needs_engagement
        and not needs_head_ci
        and not needs_head_sha
        and not force
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
                        "force": force,
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
                "force": force,
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
            "force": force,
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
    """Discover changed PRs and enqueue per-PR sync tasks.

    Discovery runs in two modes, under a per-repo lock:
    - fresh sweep: compute a sliding cutoff with watermark overlap;
    - continuation: resume from persisted cursor + fixed cutoff.
    """
    repo = Repository.objects.get(id=int(repo_id))

    with repo_advisory_lock(repo.id) as acquired:
        if not acquired:
            log.info("sync_repo_since: lock not acquired; skipping repo=%s/%s", repo.owner, repo.name)
            return {"skipped": True, "reason": "lock_not_acquired"}

        client = GitHubClient(operation="syncer_repo_discovery", owner=repo.owner, repo=repo.name)
        rate_events: list[dict] = []
        state, _ = RepoDiscoveryState.objects.get_or_create(repository=repo)
        state.mark_attempted()

        # Parameters
        lim = int(limit) if isinstance(limit, int) else int(getattr(settings, "SYNCER_DISCOVERY_LIMIT", 30))
        st: list[str]
        if states is None:
            st = [s for s in getattr(settings, "SYNCER_DISCOVERY_STATES_DEFAULT", ["OPEN"]) if s]
        else:
            st = [str(s).upper() for s in states]
        tk = int(timelineK) if isinstance(timelineK, int) else int(getattr(settings, "SYNCER_TIMELINE_K_DEFAULT", 150))
        cm = int(commitsM) if isinstance(commitsM, int) else int(getattr(settings, "SYNCER_COMMITS_M_DEFAULT", 15))

        def _compute_base_fresh_cutoff() -> timezone.datetime:
            if since_iso:
                base_cutoff = _parse_iso_awareness(since_iso) or timezone.now()
            else:
                lookback_min = int(getattr(settings, "SYNCER_DISCOVERY_LOOKBACK_MINUTES", 60))
                base_cutoff = timezone.now() - timedelta(minutes=lookback_min)
                if timezone.is_naive(base_cutoff):
                    base_cutoff = timezone.make_aware(base_cutoff)
            return base_cutoff

        def _compute_fresh_scan_start_cutoff(*, base_cutoff: datetime) -> timezone.datetime:
            overlap_seconds = int(getattr(settings, "SYNCER_DISCOVERY_OVERLAP_SECONDS", 300))
            if state.last_successful_cutoff_at is not None:
                watermark_overlap_cutoff = state.last_successful_cutoff_at - timedelta(seconds=max(0, overlap_seconds))
                return min(base_cutoff, watermark_overlap_cutoff)
            return base_cutoff

        # Determine mode/cutoff.
        mode: str
        discovery_after: Optional[str]
        success_cutoff: datetime
        if state.continuation_cutoff_at and state.continuation_cursor:
            mode = "continuation"
            effective_cutoff = state.continuation_cutoff_at
            discovery_after = state.continuation_cursor
            success_cutoff = effective_cutoff
        else:
            mode = "fresh"
            fresh_base_cutoff = _compute_base_fresh_cutoff()
            effective_cutoff = _compute_fresh_scan_start_cutoff(base_cutoff=fresh_base_cutoff)
            discovery_after = None
            success_cutoff = fresh_base_cutoff

        cutoff_iso = effective_cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            discovery = client.discover_changed_pr_numbers(
                owner=repo.owner,
                name=repo.name,
                since_iso=cutoff_iso,
                states=st,
                limit=lim,
                after=discovery_after,
            )
        except Exception:
            if mode != "continuation":
                raise
            # Stale/corrupt continuation cursors should fail-safe: clear and restart fresh.
            log.warning(
                "sync_repo_since: continuation discovery failed, resetting continuation repo=%s/%s",
                repo.owner,
                repo.name,
                exc_info=True,
            )
            state.continuation_cutoff_at = None
            state.continuation_cursor = None
            state.continuation_started_at = None
            state.save(update_fields=["continuation_cutoff_at", "continuation_cursor", "continuation_started_at", "updated_at"])
            mode = "fresh_recovery"
            fresh_base_cutoff = _compute_base_fresh_cutoff()
            effective_cutoff = _compute_fresh_scan_start_cutoff(base_cutoff=fresh_base_cutoff)
            success_cutoff = fresh_base_cutoff
            cutoff_iso = effective_cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
            discovery = client.discover_changed_pr_numbers(
                owner=repo.owner,
                name=repo.name,
                since_iso=cutoff_iso,
                states=st,
                limit=lim,
                after=None,
            )
        numbers = discovery.numbers

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

        scan_complete = bool(discovery.reached_cutoff or discovery.next_cursor is None)
        if scan_complete:
            state.mark_success(cutoff_at=success_cutoff)
        else:
            state.set_continuation(cutoff_at=effective_cutoff, cursor=discovery.next_cursor)

        enqueued = 0
        threshold = int(getattr(settings, "SYNCER_RATE_REMAINING_MIN", 200))
        low_budget = isinstance(remaining, int) and remaining <= threshold
        continuation_scheduled = False
        continuation_reason: Optional[str] = None
        continuation_debounce_key: Optional[str] = None

        def _schedule_repo_continuation(*, debounce_key: Optional[str], eta: Optional[datetime]) -> bool:
            if not debounce_key or eta is None:
                return False
            if not debounce_repo_schedule(repo.id, debounce_key):
                return False
            try:
                sig = sync_repo_since_task.s(
                    repo.id,
                    since_iso=since_iso,
                    limit=lim,
                    states=st,
                    timelineK=tk,
                    commitsM=cm,
                    dry_run=dry_run,
                )
                enqueue_with_parent(sig, self.request, eta=eta)
                return True
            except Exception:
                return False

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
            if not scan_complete:
                continuation_debounce_key = reset_at if isinstance(reset_at, str) else None
                continuation_scheduled = _schedule_repo_continuation(debounce_key=continuation_debounce_key, eta=eta)
                if continuation_scheduled:
                    continuation_reason = "low_budget"
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

            # If discovery did not reach cutoff, schedule a continuation even when budget is healthy.
            if not scan_complete:
                cap_delay_seconds = int(getattr(settings, "SYNCER_DISCOVERY_CONTINUATION_DELAY_SECONDS", 5))
                cap_eta = timezone.now() + timedelta(seconds=max(1, cap_delay_seconds))
                continuation_debounce_key = f"cap:{cutoff_iso}:{discovery.next_cursor or ''}"
                continuation_scheduled = _schedule_repo_continuation(
                    debounce_key=continuation_debounce_key,
                    eta=cap_eta,
                )
                if continuation_scheduled:
                    continuation_reason = "cap_exhausted"
        log.info(
            "sync_repo_since: repo=%s/%s mode=%s since=%s discovered=%s enqueued=%s remaining=%s resetAt=%s complete=%s next_cursor=%s continuation_scheduled=%s continuation_reason=%s",
            repo.owner,
            repo.name,
            mode,
            cutoff_iso,
            len(numbers),
            enqueued,
            rl.get("remaining"),
            rl.get("resetAt"),
            scan_complete,
            bool(discovery.next_cursor),
            continuation_scheduled,
            continuation_reason,
        )
        return {
            "skipped": False,
            "repo": f"{repo.owner}/{repo.name}",
            "mode": mode,
            "since": cutoff_iso,
            "cutoff_iso": cutoff_iso,
            "discovered": len(numbers),
            "enqueued": enqueued,
            "rate_limit": rl,
            "low_budget": bool(low_budget),
            "scan_complete": scan_complete,
            "reached_cutoff": bool(discovery.reached_cutoff),
            "hit_limit": bool(discovery.hit_limit),
            "next_cursor": discovery.next_cursor,
            "continuation_scheduled": continuation_scheduled,
            "continuation_reason": continuation_reason,
            "continuation_debounce_key": continuation_debounce_key,
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
    client = GitHubClient(operation="syncer_ci_read", owner=repo.owner, repo=repo.name)

    rate_events: list[dict] = []
    per_sha_cap = 50

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
            "results_by_result": {},
            "per_sha_results": [],
            "per_sha_results_truncated": False,
            "per_sha_results_cap": per_sha_cap,
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
    per_sha_results: list[dict[str, Any]] = []
    results_by_result: dict[str, int] = {}

    for sha in todo:
        # Check budget before each SHA
        rl_now = client.get_last_rate_limit() or {}
        remaining_now = rl_now.get("remaining") if isinstance(rl_now, dict) else None
        reset_at = rl_now.get("resetAt") if isinstance(rl_now, dict) else None
        if isinstance(remaining_now, int) and remaining_now <= threshold:
            remaining = [s for s in todo if s not in done]
            return _defer(reset_at, remaining)

        if dry_run:
            if len(per_sha_results) < per_sha_cap:
                per_sha_results.append({"sha": sha, "result": "dry_run"})
            results_by_result["dry_run"] = results_by_result.get("dry_run", 0) + 1
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
        result = str(res.get("result") or "ok")
        record_ci_sha_fetch(pr=pr, sha=sha, result=result)
        if len(per_sha_results) < per_sha_cap:
            per_sha_results.append(
                {
                    "sha": sha,
                    "result": result,
                    "found_commit": bool(res.get("found_commit")),
                    "found_contexts": bool(res.get("found_contexts")),
                    "counts": {k: int(res.get(k, 0)) for k in total_counts.keys()},
                }
            )
        results_by_result[result] = results_by_result.get(result, 0) + 1
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
        "results_by_result": results_by_result,
        "per_sha_results": per_sha_results,
        "per_sha_results_truncated": len(todo) > per_sha_cap,
        "per_sha_results_cap": per_sha_cap,
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

    # Max age GitHub is allowed to report a CI row as pending before we stop polling it.
    if max_pending_hours is None:
        max_pending_hours = int(getattr(settings, "SYNCER_PENDING_CI_MAX_AGE_HOURS", 48))
    max_age = timedelta(hours=max_pending_hours)

    # Identify PRs that currently have any pending CI.
    cr_origin = Coalesce("gh_started_at", "gh_completed_at", "created_at")
    sc_origin = Coalesce("gh_created_at", "created_at")
    ccr_origin = Coalesce("gh_started_at", "gh_completed_at", "created_at")
    csc_origin = Coalesce("gh_created_at", "created_at")
    pending_cr = CheckRun.objects.filter(pull_request=OuterRef("pk")).exclude(status="COMPLETED")
    pending_sc = StatusContext.objects.filter(pull_request=OuterRef("pk"), state="PENDING")
    pending_ccr = CommitCheckRun.objects.filter(
        repository=repo,
        head_sha=OuterRef("head_sha"),
    ).exclude(status="COMPLETED")
    pending_csc = CommitStatusContext.objects.filter(
        repository=repo,
        head_sha=OuterRef("head_sha"),
        state="PENDING",
    )
    eligible_pending_cr = pending_cr.filter(
        models.Q(last_synced_at__isnull=True)
        | models.Q(
            last_synced_at__lt=(
                models.ExpressionWrapper(
                    cr_origin + models.Value(max_age),
                    output_field=models.DateTimeField(),
                )
            )
        )
    )
    eligible_pending_sc = pending_sc.filter(
        models.Q(last_synced_at__isnull=True)
        | models.Q(
            last_synced_at__lt=(
                models.ExpressionWrapper(
                    sc_origin + models.Value(max_age),
                    output_field=models.DateTimeField(),
                )
            )
        )
    )
    eligible_pending_ccr = pending_ccr.filter(
        models.Q(last_synced_at__isnull=True)
        | models.Q(
            last_synced_at__lt=(
                models.ExpressionWrapper(
                    ccr_origin + models.Value(max_age),
                    output_field=models.DateTimeField(),
                )
            )
        )
    )
    eligible_pending_csc = pending_csc.filter(
        models.Q(last_synced_at__isnull=True)
        | models.Q(
            last_synced_at__lt=(
                models.ExpressionWrapper(
                    csc_origin + models.Value(max_age),
                    output_field=models.DateTimeField(),
                )
            )
        )
    )
    # Identify PRs whose head SHA has no CI contexts stored at all.
    head_cr = CheckRun.objects.filter(pull_request=OuterRef("pk"), head_sha=OuterRef("head_sha"))
    head_sc = StatusContext.objects.filter(pull_request=OuterRef("pk"), head_sha=OuterRef("head_sha"))
    head_ccr = CommitCheckRun.objects.filter(repository=repo, head_sha=OuterRef("head_sha"))
    head_csc = CommitStatusContext.objects.filter(repository=repo, head_sha=OuterRef("head_sha"))

    has_recent_pending_ci = (
        Exists(eligible_pending_cr) | Exists(eligible_pending_sc) | Exists(eligible_pending_ccr) | Exists(eligible_pending_csc)
    )
    prs_qs = (
        PullRequest.objects.filter(repository=repo)
        .annotate(
            has_head_cr=Exists(head_cr),
            has_head_sc=Exists(head_sc),
            has_head_ccr=Exists(head_ccr),
            has_head_csc=Exists(head_csc),
        )
        .filter(
            has_recent_pending_ci
            | (
                Q(head_sha__isnull=False)
                & ~Q(head_sha="")
                & Q(has_head_cr=False)
                & Q(has_head_sc=False)
                & Q(has_head_ccr=False)
                & Q(has_head_csc=False)
                & ~Q(head_ci_state__iexact="UNAVAILABLE")
            )
        )
        .annotate(state_rank=models.Case(models.When(state="open", then=0), default=1, output_field=models.IntegerField()))
        .only("id", "number", "state", "gh_updated_at", "head_sha", "head_ci_state")
        .order_by("state_rank", "gh_updated_at", "id")
    )

    prs_enqueued = 0
    shas_enqueued = 0
    prs_missing_head_ci = 0
    shas_missing_head_ci = 0
    shas_skipped_backoff = 0
    prs_skipped_backoff = 0
    per_pr: list[dict[str, Any]] = []
    skipped_stale = 0
    skipped_no_eligible = 0
    skipped_unavailable_head_ci = 0
    prs_scanned_total = 0
    prs_seen_pending_or_missing_head = 0

    now = timezone.now()

    # Gather actionable PRs by scanning until we have max_prs_int eligible.
    actionable_found = 0
    for pr in prs_qs.iterator():
        prs_scanned_total += 1
        prs_seen_pending_or_missing_head += 1
        missing_head_ci = (
            bool(getattr(pr, "head_sha", None))
            and not bool(getattr(pr, "has_head_cr", False))
            and not bool(getattr(pr, "has_head_sc", False))
            and not bool(getattr(pr, "has_head_ccr", False))
            and not bool(getattr(pr, "has_head_csc", False))
            and str(getattr(pr, "head_ci_state", "")).upper() != "UNAVAILABLE"
        )
        if (
            bool(getattr(pr, "head_sha", None))
            and not bool(getattr(pr, "has_head_cr", False))
            and not bool(getattr(pr, "has_head_sc", False))
            and not bool(getattr(pr, "has_head_ccr", False))
            and not bool(getattr(pr, "has_head_csc", False))
            and str(getattr(pr, "head_ci_state", "")).upper() == "UNAVAILABLE"
        ):
            skipped_unavailable_head_ci += 1

        # Pending CheckRuns with acceptable "pending duration".
        cr_qs = (
            CheckRun.objects.filter(pull_request=pr)
            .order_by(
                "head_sha",
                "name",
                Coalesce("gh_completed_at", "gh_started_at").desc(),
                "-id",
            )
            .distinct("head_sha", "name")
        )
        eligible_cr_shas: set[str] = set()
        has_recent_pending = False
        for cr in cr_qs:
            if cr.status == "COMPLETED":
                continue
            origin = cr.gh_started_at or cr.gh_completed_at or cr.created_at
            if origin is None:
                origin = now
            if cr.last_synced_at is None or (cr.last_synced_at - origin) < max_age:
                has_recent_pending = True
                # head_sha should always be present for ingested CI rows.
                eligible_cr_shas.add(cr.head_sha)
        if pr.head_sha:
            commit_cr_qs = (
                CommitCheckRun.objects.filter(repository=pr.repository, head_sha=pr.head_sha)
                .order_by(
                    "head_sha",
                    "name",
                    Coalesce("gh_completed_at", "gh_started_at").desc(),
                    "-id",
                )
                .distinct("head_sha", "name")
            )
            for cr in commit_cr_qs:
                if cr.status == "COMPLETED":
                    continue
                origin = cr.gh_started_at or cr.gh_completed_at or cr.created_at
                if origin is None:
                    origin = now
                if cr.last_synced_at is None or (cr.last_synced_at - origin) < max_age:
                    has_recent_pending = True
                    eligible_cr_shas.add(cr.head_sha)

        # Pending StatusContexts with acceptable "pending duration".
        sc_qs = (
            StatusContext.objects.filter(pull_request=pr)
            .order_by("head_sha", "name", "-gh_created_at", "-id")
            .distinct("head_sha", "name")
        )
        eligible_sc_shas: set[str] = set()
        for sc in sc_qs:
            if sc.state != "PENDING":
                continue
            origin_sc = sc.gh_created_at or sc.created_at
            if origin_sc is None:
                origin_sc = now
            if sc.last_synced_at is None or (sc.last_synced_at - origin_sc) < max_age:
                has_recent_pending = True
                # head_sha should always be present for ingested CI rows.
                eligible_sc_shas.add(sc.head_sha)
        if pr.head_sha:
            commit_sc_qs = (
                CommitStatusContext.objects.filter(repository=pr.repository, head_sha=pr.head_sha)
                .order_by("head_sha", "name", "-gh_created_at", "-id")
                .distinct("head_sha", "name")
            )
            for sc in commit_sc_qs:
                if sc.state != "PENDING":
                    continue
                origin_sc = sc.gh_created_at or sc.created_at
                if origin_sc is None:
                    origin_sc = now
                if sc.last_synced_at is None or (sc.last_synced_at - origin_sc) < max_age:
                    has_recent_pending = True
                    eligible_sc_shas.add(sc.head_sha)

        shas: list[str] = []
        item_reason = "pending_ci"
        if missing_head_ci and pr.head_sha:
            shas.append(pr.head_sha)
            item_reason = "missing_head_ci"
        for sha in sorted(eligible_cr_shas | eligible_sc_shas):
            if sha not in shas:
                shas.append(sha)
        if not shas:
            if not has_recent_pending:
                skipped_stale += 1
            else:
                skipped_no_eligible += 1
            if actionable_found >= max_prs_int:
                break
            continue
        shas = shas[:max_shas_int]
        pre_gate_count = len(shas)
        state_by_sha = {
            row.sha: row
            for row in CIShaFetchState.objects.filter(
                repository=pr.repository,
                sha__in=shas,
            )
        }
        shas = [
            sha
            for sha in shas
            if should_enqueue_ci_sha_with_state(
                pr=pr,
                sha=sha,
                state=state_by_sha.get(sha),
                reason="refresh_pending_ci",
            )
        ]

        # Enqueue CI refresh for these SHAs.
        if not shas:
            if pre_gate_count:
                prs_skipped_backoff += 1
                shas_skipped_backoff += pre_gate_count
            continue
        if pre_gate_count > len(shas):
            shas_skipped_backoff += pre_gate_count - len(shas)
        actionable_found += 1
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
        if missing_head_ci and pr.head_sha:
            prs_missing_head_ci += 1
            shas_missing_head_ci += 1
        per_pr.append(
            {
                "number": int(pr.number),
                "shas": shas,
                "task_id": async_res.id,
                "reason": item_reason,
            }
        )
        if actionable_found >= max_prs_int:
            break

    return {
        "repo": f"{repo.owner}/{repo.name}",
        "repo_id": repo.id,
        "prs_considered": actionable_found,
        "prs_enqueued": prs_enqueued,
        "shas_enqueued": shas_enqueued,
        "prs_missing_head_ci": prs_missing_head_ci,
        "shas_missing_head_ci": shas_missing_head_ci,
        "prs_skipped_backoff": prs_skipped_backoff,
        "shas_skipped_backoff": shas_skipped_backoff,
        "backlog_prs_actionable_scanned": actionable_found,
        "prs_scanned_total": prs_scanned_total,
        "prs_seen_pending_or_missing_head": prs_seen_pending_or_missing_head,
        "max_prs": max_prs_int,
        "max_shas_per_pr": max_shas_int,
        "max_pending_hours": int(max_pending_hours),
        "prs_skipped_stale": skipped_stale,
        "prs_skipped_no_eligible": skipped_no_eligible,
        "prs_skipped_unavailable_head_ci": skipped_unavailable_head_ci,
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
