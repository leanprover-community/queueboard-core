from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Dict, Optional, Sequence
from datetime import datetime, timedelta

from celery import shared_task
from django.db import connection, models
from django.db.models.functions import Coalesce
from dateutil import parser as dtparser
from django.utils import timezone
from django.conf import settings

from core.models import Repository
from syncer.models import PullRequest, RepoDiscoveryState
from syncer.services.ci_sha_task_runner import run_ci_sync_for_pr_shas
from syncer.services.github_client import GitHubClient
from syncer.services.pr_sync_service import PRSyncService
from core.utils.locks import repo_advisory_lock
from syncer.services.rate_budget import debounce_repo_schedule
from syncer.services.ci_backoff import record_ci_sha_fetch, should_enqueue_ci_sha_with_state
from syncer.services.task_dedupe import (
    claim_enqueue_slot,
    claim_runtime_slot,
    sync_ci_enqueue_key,
    sync_pr_enqueue_key,
    sync_pr_runtime_key,
)
from syncer.models import CIShaFetchState, CommitCheckRun, CommitStatusContext
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

    if not force:
        runtime_ttl = int(getattr(settings, "SYNCER_SYNC_PR_RUNTIME_DEDUPE_TTL_SECONDS", 300))
        runtime_key = sync_pr_runtime_key(repo_id=repo.id, number=int(number))
        if not claim_runtime_slot(key=runtime_key, ttl_seconds=runtime_ttl):
            return {
                "skipped": True,
                "status": "runtime_deduped",
                "reason": "recently_processed",
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
            }

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
    # Even when updatedAt is unchanged, we may need to sync to fill head CI rollup or backfill history.
    if (
        pr_db
        and last_synced_cutoff
        and gh_updated
        and gh_updated <= last_synced_cutoff
        and not needs_state_refresh
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
            # Use the success_cutoff stored when the fresh scan spawned this continuation
            # (fresh_base_cutoff at that time), so completing the catch-up advances the
            # watermark to near-now rather than to the stale continuation_cutoff_at.
            success_cutoff = state.continuation_success_cutoff or effective_cutoff
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

        threshold = int(getattr(settings, "SYNCER_RATE_REMAINING_MIN", 200))
        low_budget = isinstance(remaining, int) and remaining <= threshold

        enqueued = 0
        prs_skipped_dedupe = 0
        # Discovered numbers we could not act on this tick (rate budget exhausted, or
        # low_budget deferral). These are NOT lost: we hold the watermark below them so a
        # later tick rediscovers and drains them. Closed PRs have a frozen updatedAt, so a
        # number stepped over here would otherwise never be revisited.
        undrained = 0
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

        # --- Enqueue per-PR sync tasks, sized by remaining rate budget ---
        if low_budget:
            # Cannot afford any per-PR syncs this tick; defer the whole batch.
            undrained = len(numbers)
        else:
            batch_max = int(getattr(settings, "SYNCER_REPO_ENQUEUE_BATCH_MAX", 30))
            est_cost = int(getattr(settings, "SYNCER_EST_COST_PER_PR", 150))
            if isinstance(remaining, int):
                allowed = max(0, remaining - threshold)
                dynamic_cap = allowed // max(1, est_cost)
                if dynamic_cap <= 0:
                    budget = min(batch_max, len(numbers), 1)
                else:
                    budget = min(len(numbers), batch_max, int(dynamic_cap))
            else:
                budget = min(len(numbers), batch_max)

            pr_dedupe_ttl = int(getattr(settings, "SYNCER_SYNC_PR_DEDUPE_TTL_SECONDS", 300))
            # Iterate ALL discovered numbers but spend the budget only on *new* enqueues.
            # Dedupe-skips are already in flight and cost nothing, so they must not consume
            # a slot -- otherwise the budget is burnt on the positional head on every
            # re-scan and the undrained tail never gets reached.
            for idx, num in enumerate(numbers):
                if enqueued >= budget:
                    undrained = len(numbers) - idx
                    break
                pr_dedupe_key = sync_pr_enqueue_key(repo_id=repo.id, number=int(num))
                if not claim_enqueue_slot(key=pr_dedupe_key, ttl_seconds=pr_dedupe_ttl):
                    prs_skipped_dedupe += 1
                    continue
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

        # --- Watermark / continuation state ---
        # Advance the watermark ONLY when the scan reached the cutoff AND every discovered
        # number was covered (enqueued or already in flight). If any were left undrained,
        # hold the watermark and clear any continuation cursor so the next tick re-scans
        # this same window and drains the tail -- never step the watermark past a
        # discovered-but-un-enqueued PR.
        if scan_complete and undrained == 0:
            state.mark_success(cutoff_at=success_cutoff)
        elif not scan_complete:
            if mode in ("fresh", "fresh_recovery"):
                # Store fresh_base_cutoff as the intended watermark target so that when this
                # continuation chain eventually completes it advances to near-now, not to the
                # stale continuation_cutoff_at that would re-trap the watermark.
                state.set_continuation(cutoff_at=effective_cutoff, cursor=discovery.next_cursor, success_cutoff=fresh_base_cutoff)
            else:
                state.set_continuation(cutoff_at=effective_cutoff, cursor=discovery.next_cursor)
        elif state.continuation_cursor is not None or state.continuation_cutoff_at is not None:
            # scan_complete but undrained: drop any continuation cursor so the next tick is a
            # fresh re-scan of the (held-watermark) window rather than a forward resume.
            state.continuation_cutoff_at = None
            state.continuation_cursor = None
            state.continuation_started_at = None
            state.continuation_success_cutoff = None
            state.save(
                update_fields=[
                    "continuation_cutoff_at",
                    "continuation_cursor",
                    "continuation_started_at",
                    "continuation_success_cutoff",
                    "updated_at",
                ]
            )

        # --- Schedule a near-term continuation when work remains in this window ---
        if not scan_complete or undrained > 0:
            cap_delay_seconds = int(getattr(settings, "SYNCER_DISCOVERY_CONTINUATION_DELAY_SECONDS", 5))
            if low_budget:
                # Resume after the rate window resets rather than hammering the limit.
                eta = None
                if isinstance(reset_at, str):
                    try:
                        rdt = dtparser.isoparse(reset_at)
                        if timezone.is_naive(rdt):
                            rdt = timezone.make_aware(rdt)
                        eta = rdt + timedelta(seconds=5)
                    except Exception:
                        eta = None
                continuation_debounce_key = reset_at if isinstance(reset_at, str) else None
                reason = "low_budget"
            elif not scan_complete:
                eta = timezone.now() + timedelta(seconds=max(1, cap_delay_seconds))
                continuation_debounce_key = f"cap:{cutoff_iso}:{discovery.next_cursor or ''}"
                reason = "cap_exhausted"
            else:
                # Drain the undrained tail promptly -- within the per-PR enqueue dedupe TTL,
                # so already-enqueued numbers are skipped and the budget reaches the tail.
                # The debounce key varies with drain progress so each successive tick can
                # schedule the next drain.
                eta = timezone.now() + timedelta(seconds=max(1, cap_delay_seconds))
                continuation_debounce_key = f"drain:{cutoff_iso}:{enqueued}:{prs_skipped_dedupe}"
                reason = "undrained_tail"
            continuation_scheduled = _schedule_repo_continuation(debounce_key=continuation_debounce_key, eta=eta)
            if continuation_scheduled:
                continuation_reason = reason
        log.info(
            "sync_repo_since: repo=%s/%s mode=%s since=%s discovered=%s enqueued=%s undrained=%s remaining=%s resetAt=%s complete=%s next_cursor=%s continuation_scheduled=%s continuation_reason=%s",
            repo.owner,
            repo.name,
            mode,
            cutoff_iso,
            len(numbers),
            enqueued,
            undrained,
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
            "prs_skipped_dedupe": prs_skipped_dedupe,
            "undrained": undrained,
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
    trigger_analyzer_after_sync: bool = False,
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
                    trigger_analyzer_after_sync=trigger_analyzer_after_sync,
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
            "analyzer_enqueued": False,
            "analyzer_task_id": None,
        }

    threshold = int(getattr(settings, "SYNCER_RATE_REMAINING_MIN", 200))
    max_pages = (
        int(max_pages_per_sha) if isinstance(max_pages_per_sha, int) else int(getattr(settings, "SYNCER_CI_BY_SHA_PAGES", 1))
    )
    require_assoc = bool(require_pr_association) if require_pr_association is not None else False

    def _record_result(sha: str, result: str) -> None:
        record_ci_sha_fetch(pr=pr, sha=sha, result=result)

    exec_res = run_ci_sync_for_pr_shas(
        pr=pr,
        shas=shas,
        client=client,
        max_pages_per_sha=max_pages,
        dry_run=dry_run,
        require_pr_association=require_assoc,
        budget_threshold=threshold,
        per_sha_cap=per_sha_cap,
        rate_log=rate_log,
        on_sha_result=_record_result,
    )
    if str(exec_res.get("status") or "") == "deferred":
        return _defer(
            exec_res.get("reset_at") if isinstance(exec_res.get("reset_at"), str) else None,
            [str(s) for s in exec_res.get("remaining_shas", []) if isinstance(s, str)],
        )

    rl_final = client.get_last_rate_limit() or {}
    analyzer_enqueued = False
    analyzer_task_id: str | None = None
    if trigger_analyzer_after_sync and not dry_run:
        try:
            from analyzer.tasks import process_pr_task

            async_res = enqueue_with_parent(process_pr_task.s(int(pr.id)), self.request)
            analyzer_enqueued = True
            analyzer_task_id = str(async_res.id) if getattr(async_res, "id", None) else None
        except Exception:
            log.exception(
                "sync_ci_for_shas_task: failed to enqueue analyzer.process_pr for repo=%s/%s pr=%s",
                repo.owner,
                repo.name,
                number,
            )

    return {
        "status": "ok",
        "repo": f"{repo.owner}/{repo.name}",
        "number": int(number),
        "shas_done": exec_res.get("done", []),
        "counts": exec_res.get("counts", {}),
        "results_by_result": exec_res.get("results_by_result", {}),
        "per_sha_results": exec_res.get("per_sha_results", []),
        "per_sha_results_truncated": bool(exec_res.get("per_sha_results_truncated")),
        "per_sha_results_cap": per_sha_cap,
        "rate_limit": rl_final,
        "rate_events": rate_events,
        "analyzer_enqueued": analyzer_enqueued,
        "analyzer_task_id": analyzer_task_id,
    }


@shared_task(name="syncer.sync_ci_for_repo_shas", bind=True)
def sync_ci_for_repo_shas_task(  # type: ignore[no-redef]
    self,
    repo_id: int,
    *,
    shas: Sequence[str],
    max_pages_per_sha: Optional[int] = None,
    dry_run: bool = False,
    require_pr_association: Optional[bool] = None,
    trigger_analyzer_after_sync: bool = False,
) -> Dict[str, Any]:
    """Fetch CI for repository SHAs without requiring PR fanout at enqueue time."""
    repo = Repository.objects.get(id=int(repo_id))
    client = GitHubClient(operation="syncer_ci_read", owner=repo.owner, repo=repo.name)
    per_sha_cap = 50
    threshold = int(getattr(settings, "SYNCER_RATE_REMAINING_MIN", 200))
    max_pages = (
        int(max_pages_per_sha) if isinstance(max_pages_per_sha, int) else int(getattr(settings, "SYNCER_CI_BY_SHA_PAGES", 1))
    )
    require_assoc = bool(require_pr_association) if require_pr_association is not None else False

    input_shas = [sha for sha in shas if sha]
    unique_shas = list(dict.fromkeys(input_shas))
    rate_events: list[dict[str, Any]] = []

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

    # Resolve impacted PRs using historical revision heads when available, with
    # open-head fallback for recently-updated PRs whose revisions may not exist yet.
    by_sha_pr_ids: dict[str, set[int]] = {}
    impacted_pr_ids: set[int] = set()
    if unique_shas:
        try:
            from analyzer.models import PRRevision

            for pr_id, head_sha in PRRevision.objects.filter(
                pull_request__repository=repo,
                head_sha__in=unique_shas,
            ).values_list("pull_request_id", "head_sha"):
                if not head_sha:
                    continue
                by_sha_pr_ids.setdefault(str(head_sha), set()).add(int(pr_id))
                impacted_pr_ids.add(int(pr_id))
        except Exception:
            log.exception("sync_ci_for_repo_shas_task: failed PRRevision lookup for repo=%s/%s", repo.owner, repo.name)

    for pr_id, head_sha in PullRequest.objects.filter(
        repository=repo,
        state="open",
        head_sha__in=unique_shas,
    ).values_list("id", "head_sha"):
        if not head_sha:
            continue
        by_sha_pr_ids.setdefault(str(head_sha), set()).add(int(pr_id))
        impacted_pr_ids.add(int(pr_id))

    pr_by_id = {
        int(pr.id): pr
        for pr in PullRequest.objects.filter(id__in=impacted_pr_ids).only(
            "id",
            "number",
            "repository_id",
            "head_repo_owner_login",
            "head_repo_name",
            "state",
        )
    }
    pr_shas: dict[int, list[str]] = {}
    unassociated_shas: list[str] = []
    for sha in unique_shas:
        pr_ids = sorted(by_sha_pr_ids.get(sha) or [])
        if not pr_ids:
            unassociated_shas.append(sha)
            continue
        for pr_id in pr_ids:
            if pr_id not in pr_by_id:
                continue
            pr_shas.setdefault(pr_id, [])
            if sha not in pr_shas[pr_id]:
                pr_shas[pr_id].append(sha)

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
                sig = sync_ci_for_repo_shas_task.s(
                    repo.id,
                    shas=list(remaining_shas),
                    max_pages_per_sha=max_pages_per_sha,
                    dry_run=dry_run,
                    require_pr_association=require_pr_association,
                    trigger_analyzer_after_sync=trigger_analyzer_after_sync,
                )
                enqueue_with_parent(sig, self.request, eta=eta)
            except Exception:
                pass
        rl = client.get_last_rate_limit() or {}
        return {
            "status": "deferred",
            "repo": f"{repo.owner}/{repo.name}",
            "repo_id": repo.id,
            "remaining_shas": list(remaining_shas),
            "unassociated_shas": unassociated_shas,
            "impacted_pr_ids": sorted(impacted_pr_ids),
            "impacted_pr_count": len(impacted_pr_ids),
            "counts": {},
            "results_by_result": {},
            "per_sha_results": [],
            "per_sha_results_truncated": False,
            "per_sha_results_cap": per_sha_cap,
            "rate_limit": rl,
            "rate_events": rate_events,
            "analyzer_enqueued": 0,
            "analyzer_task_ids": [],
        }

    total_counts = {"checkruns_created": 0, "checkruns_updated": 0, "status_created": 0, "status_updated": 0}
    results_by_result: dict[str, int] = {}
    per_sha_results: list[dict[str, Any]] = []
    shas_done: list[str] = []
    shas_seen: set[str] = set()

    for pr_id in sorted(pr_shas.keys()):
        pr = pr_by_id[pr_id]
        pr_exec = run_ci_sync_for_pr_shas(
            pr=pr,
            shas=pr_shas.get(pr_id, []),
            client=client,
            max_pages_per_sha=max_pages,
            dry_run=dry_run,
            require_pr_association=require_assoc,
            budget_threshold=threshold,
            per_sha_cap=per_sha_cap,
            rate_log=rate_log,
            on_sha_result=lambda sha, result, p=pr: record_ci_sha_fetch(pr=p, sha=sha, result=result),
        )
        if str(pr_exec.get("status") or "") == "deferred":
            remaining = [sha for sha in unique_shas if sha not in shas_seen]
            reset_at = pr_exec.get("reset_at") if isinstance(pr_exec.get("reset_at"), str) else None
            return _defer(reset_at, remaining)

        for key in total_counts.keys():
            total_counts[key] += int((pr_exec.get("counts") or {}).get(key, 0))
        for result_key, count in (pr_exec.get("results_by_result") or {}).items():
            if not isinstance(result_key, str):
                continue
            results_by_result[result_key] = results_by_result.get(result_key, 0) + int(count)
        for item in pr_exec.get("per_sha_results", []):
            if not isinstance(item, dict):
                continue
            if len(per_sha_results) >= per_sha_cap:
                continue
            row = dict(item)
            row["pr_number"] = int(pr.number)
            per_sha_results.append(row)
        for done_sha in pr_exec.get("done", []):
            if not isinstance(done_sha, str):
                continue
            shas_seen.add(done_sha)
            if done_sha not in shas_done:
                shas_done.append(done_sha)

    analyzer_task_ids: list[str] = []
    if trigger_analyzer_after_sync and not dry_run:
        try:
            from analyzer.tasks import process_pr_task

            for pr_id in sorted(impacted_pr_ids):
                async_res = enqueue_with_parent(process_pr_task.s(int(pr_id)), self.request)
                if getattr(async_res, "id", None):
                    analyzer_task_ids.append(str(async_res.id))
        except Exception:
            log.exception("sync_ci_for_repo_shas_task: failed to enqueue analyzer.process_pr follow-up")

    rl_final = client.get_last_rate_limit() or {}
    return {
        "status": "ok",
        "repo": f"{repo.owner}/{repo.name}",
        "repo_id": repo.id,
        "input_shas": unique_shas,
        "shas_done": shas_done,
        "unassociated_shas": unassociated_shas,
        "impacted_pr_ids": sorted(impacted_pr_ids),
        "impacted_pr_count": len(impacted_pr_ids),
        "counts": total_counts,
        "results_by_result": results_by_result,
        "per_sha_results": per_sha_results,
        "per_sha_results_truncated": len(unique_shas) > per_sha_cap,
        "per_sha_results_cap": per_sha_cap,
        "rate_limit": rl_final,
        "rate_events": rate_events,
        "analyzer_enqueued": len(analyzer_task_ids),
        "analyzer_task_ids": analyzer_task_ids,
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
    - Consider PRs in the given repo that currently have any non-terminal commit-scoped CI:
      - CommitCheckRun.status != COMPLETED
      - or CommitStatusContext.state == PENDING
    - For each such PR, collect head_shas for "eligible" pending CI rows:
      - If last_synced_at is NULL: always eligible (never refreshed explicitly).
      - Else, compute how long GitHub has been reporting this row as pending:
        pending_duration = last_synced_at - origin
        where origin is:
          - CommitCheckRun: gh_started_at or gh_completed_at or created_at
          - CommitStatusContext: gh_created_at
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
    ccr_origin = Coalesce("gh_started_at", "gh_completed_at", "created_at")
    csc_origin = Coalesce("gh_created_at", "created_at")
    pending_ccr = CommitCheckRun.objects.filter(
        repository=repo,
        head_sha=OuterRef("head_sha"),
    ).exclude(status="COMPLETED")
    pending_csc = CommitStatusContext.objects.filter(
        repository=repo,
        head_sha=OuterRef("head_sha"),
        state="PENDING",
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
    # Identify PRs whose head SHA has no commit-scoped CI contexts stored at all.
    head_ccr = CommitCheckRun.objects.filter(repository=repo, head_sha=OuterRef("head_sha"))
    head_csc = CommitStatusContext.objects.filter(repository=repo, head_sha=OuterRef("head_sha"))

    has_recent_pending_ci = Exists(eligible_pending_ccr) | Exists(eligible_pending_csc)
    prs_qs = (
        PullRequest.objects.filter(repository=repo)
        .annotate(
            has_head_ccr=Exists(head_ccr),
            has_head_csc=Exists(head_csc),
        )
        .filter(
            has_recent_pending_ci
            | (
                Q(head_sha__isnull=False)
                & ~Q(head_sha="")
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
    shas_skipped_dedupe = 0
    prs_skipped_dedupe = 0
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
            and not bool(getattr(pr, "has_head_ccr", False))
            and not bool(getattr(pr, "has_head_csc", False))
            and str(getattr(pr, "head_ci_state", "")).upper() != "UNAVAILABLE"
        )
        if (
            bool(getattr(pr, "head_sha", None))
            and not bool(getattr(pr, "has_head_ccr", False))
            and not bool(getattr(pr, "has_head_csc", False))
            and str(getattr(pr, "head_ci_state", "")).upper() == "UNAVAILABLE"
        ):
            skipped_unavailable_head_ci += 1

        # Pending commit-scoped CheckRuns with acceptable "pending duration".
        eligible_cr_shas: set[str] = set()
        has_recent_pending = False
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

        # Pending commit-scoped StatusContexts with acceptable "pending duration".
        eligible_sc_shas: set[str] = set()
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
        dedupe_key = sync_ci_enqueue_key(
            repo_id=repo.id,
            number=int(pr.number),
            shas=shas,
            max_pages_per_sha=pages_per_sha,
        )
        dedupe_ttl = int(getattr(settings, "SYNCER_SYNC_CI_DEDUPE_TTL_SECONDS", 300))
        if not claim_enqueue_slot(key=dedupe_key, ttl_seconds=dedupe_ttl):
            prs_skipped_dedupe += 1
            shas_skipped_dedupe += len(shas)
            continue
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
        "prs_skipped_dedupe": prs_skipped_dedupe,
        "shas_skipped_dedupe": shas_skipped_dedupe,
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


def _invalidate_queue_windows_for_ci_rows(
    *,
    check_run_ids: list[int],
    status_context_ids: list[int],
) -> set[int]:
    """Mark PRQueueWindowBuildState stale and enqueue rebuilds for any PRQueueWindow
    rows whose attribution FKs reference the given about-to-be-deleted CI row IDs.

    Must be called *before* the rows are deleted so the FK lookup still resolves.
    Returns the set of distinct PR ids affected.
    """
    from django.db.models import Q as _Q
    from analyzer.models import PRQueueWindowBuildState
    from analyzer.models.queue_window import PRQueueWindow

    if not check_run_ids and not status_context_ids:
        return set()

    fk_filter = _Q()
    if check_run_ids:
        fk_filter |= _Q(opened_by_check_run_id__in=check_run_ids)
        fk_filter |= _Q(closed_by_check_run_id__in=check_run_ids)
    if status_context_ids:
        fk_filter |= _Q(opened_by_status_context_id__in=status_context_ids)
        fk_filter |= _Q(closed_by_status_context_id__in=status_context_ids)

    affected = PRQueueWindow.objects.filter(fk_filter).values("pull_request_id", "rule_set_id").distinct()
    affected_rows = list(affected)
    if not affected_rows:
        return set()

    affected_pr_ids = list({int(row["pull_request_id"]) for row in affected_rows})
    affected_pairs = [(int(row["pull_request_id"]), int(row["rule_set_id"])) for row in affected_rows]

    # Mark build state stale for each affected (pr, ruleset) pair so the sweep
    # catches them even if the direct enqueue below fails.
    for pr_id_chunk in [affected_pr_ids[i : i + 200] for i in range(0, len(affected_pr_ids), 200)]:
        ruleset_ids_for_chunk = [rs_id for (p_id, rs_id) in affected_pairs if p_id in set(pr_id_chunk)]
        PRQueueWindowBuildState.objects.filter(
            pull_request_id__in=pr_id_chunk,
            rule_set_id__in=ruleset_ids_for_chunk,
        ).update(windows_built_at=None)

    # Enqueue direct rebuild for each affected PR (defence in depth).
    from analyzer.tasks import process_pr_task

    for pr_id in affected_pr_ids:
        process_pr_task.delay(pr_id)

    return set(affected_pr_ids)


_EXPIRE_DELETE_BATCH_SIZE = 5000


@contextmanager
def _ci_expiry_statement_timeout():
    """Cap per-statement runtime for the CI expiry passes.

    A planner regression once left this task's superseded-rows query running
    for days server-side, pinning the vacuum xmin horizon database-wide. The
    timeout turns any recurrence into a loud task failure instead.
    """
    seconds = int(getattr(settings, "SYNCER_CI_EXPIRY_STATEMENT_TIMEOUT_SECONDS", 300))
    if seconds <= 0:
        yield
        return
    with connection.cursor() as cursor:
        cursor.execute(f"SET statement_timeout = '{int(seconds)}s'")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SET statement_timeout = DEFAULT")


def _expire_ci_rows_in_batches(qs, *, model, kind: str) -> tuple[int, set[int]]:
    """Delete all rows matching ``qs`` in bounded id-cursor batches.

    For each batch: invalidate attributed queue windows first (design doc 040
    invariant I2), then delete by id. Batches keep transactions short and cap
    memory; the id cursor guarantees forward progress without rescanning.
    Correctness relies on deletions never making a previously-unmatched row
    match ``qs`` — true for both the stale-pending and superseded predicates.
    Returns (total deleted rows incl. cascades, set of affected PR ids).
    """
    deleted_total = 0
    affected_pr_ids: set[int] = set()
    last_id = 0
    while True:
        batch_ids = list(qs.filter(id__gt=last_id).order_by("id").values_list("id", flat=True)[:_EXPIRE_DELETE_BATCH_SIZE])
        if not batch_ids:
            break
        if kind == "check_run":
            affected_pr_ids |= _invalidate_queue_windows_for_ci_rows(check_run_ids=batch_ids, status_context_ids=[])
        else:
            affected_pr_ids |= _invalidate_queue_windows_for_ci_rows(check_run_ids=[], status_context_ids=batch_ids)
        deleted, _ = model.objects.filter(id__in=batch_ids).delete()
        deleted_total += deleted
        last_id = batch_ids[-1]
    return deleted_total, affected_pr_ids


@shared_task(name="syncer.expire_stale_ci_for_repo")
def expire_stale_ci_for_repo_task(  # type: ignore[no-redef]
    repo_id: int,
    *,
    stale_pending_days: int | None = None,
) -> Dict[str, Any]:
    """Delete stale and superseded CommitCheckRun / CommitStatusContext rows for one repo.

    Four passes:
    1. Stale pending check runs   – status != COMPLETED, origin older than stale_pending_days.
    2. Stale pending status ctxs  – state == PENDING, gh_created_at older than stale_pending_days.
    3. Superseded check runs      – older non-latest row per (head_sha, name).
    4. Superseded status contexts – same for CommitStatusContext, but only graphql-keyed rows
                                    (rest_id IS NULL); REST history rows are intentionally multi-row.

    Before each deletion pass, any PRQueueWindow rows whose attribution FKs reference the
    about-to-be-deleted CI row IDs are marked stale and enqueued for rebuild (see
    docs/design-decisions/040-queue-window-event-attribution.md, invariant I2).

    The superseded passes use a correlated NOT-EXISTS-style anti-join
    (``Exists(newer sibling)``), NOT ``exclude(id__in=<grouped subquery>)``:
    the latter renders as ``NOT (id IN (...))``, and once the subquery result
    outgrows work_mem Postgres re-executes it per outer row — O(n²), observed
    running for days and blocking vacuum database-wide.
    """
    from django.db.models import Exists, OuterRef

    repo = Repository.objects.get(id=int(repo_id))

    if stale_pending_days is None:
        stale_pending_days = int(getattr(settings, "SYNCER_CI_STALE_PENDING_DAYS", 30))
    stale_pending_days_int = int(stale_pending_days)

    with _ci_expiry_statement_timeout():
        # -------------------------------------------------------------- #
        # Pass 1: stale pending CommitCheckRun rows                       #
        # -------------------------------------------------------------- #
        deleted_stale_cr = 0
        prs_invalidated_stale_cr: set[int] = set()
        if stale_pending_days_int > 0:
            cutoff = timezone.now() - timedelta(days=stale_pending_days_int)
            origin = Coalesce("gh_started_at", "gh_completed_at", "created_at")
            stale_cr_qs = (
                CommitCheckRun.objects.filter(repository=repo)
                .exclude(status="COMPLETED")
                .annotate(origin=origin)
                .filter(origin__lt=cutoff)
            )
            deleted_stale_cr, prs_invalidated_stale_cr = _expire_ci_rows_in_batches(
                stale_cr_qs, model=CommitCheckRun, kind="check_run"
            )

        # -------------------------------------------------------------- #
        # Pass 2: stale pending CommitStatusContext rows                  #
        # -------------------------------------------------------------- #
        deleted_stale_sc = 0
        prs_invalidated_stale_sc: set[int] = set()
        if stale_pending_days_int > 0:
            cutoff = timezone.now() - timedelta(days=stale_pending_days_int)
            stale_sc_qs = CommitStatusContext.objects.filter(repository=repo, state="PENDING").filter(gh_created_at__lt=cutoff)
            deleted_stale_sc, prs_invalidated_stale_sc = _expire_ci_rows_in_batches(
                stale_sc_qs, model=CommitStatusContext, kind="status_context"
            )

        # -------------------------------------------------------------- #
        # Pass 3: superseded CommitCheckRun rows (older non-latest per    #
        #         (head_sha, name) within this repo)                      #
        # -------------------------------------------------------------- #
        newer_cr = CommitCheckRun.objects.filter(
            repository=repo,
            head_sha=OuterRef("head_sha"),
            name=OuterRef("name"),
            id__gt=OuterRef("id"),
        )
        superseded_cr_qs = CommitCheckRun.objects.filter(repository=repo).filter(Exists(newer_cr))
        deleted_superseded_cr, prs_invalidated_superseded_cr = _expire_ci_rows_in_batches(
            superseded_cr_qs, model=CommitCheckRun, kind="check_run"
        )

        # -------------------------------------------------------------- #
        # Pass 4: superseded CommitStatusContext rows (graphql-keyed only)#
        # -------------------------------------------------------------- #
        newer_sc = CommitStatusContext.objects.filter(
            repository=repo,
            rest_id__isnull=True,
            head_sha=OuterRef("head_sha"),
            name=OuterRef("name"),
            id__gt=OuterRef("id"),
        )
        superseded_sc_qs = CommitStatusContext.objects.filter(repository=repo, rest_id__isnull=True).filter(Exists(newer_sc))
        deleted_superseded_sc, prs_invalidated_superseded_sc = _expire_ci_rows_in_batches(
            superseded_sc_qs, model=CommitStatusContext, kind="status_context"
        )

    return {
        "repo": f"{repo.owner}/{repo.name}",
        "repo_id": repo.id,
        "stale_pending_days": stale_pending_days_int,
        "deleted_stale_pending_check_runs": deleted_stale_cr,
        "deleted_stale_pending_status_contexts": deleted_stale_sc,
        "deleted_superseded_check_runs": deleted_superseded_cr,
        "deleted_superseded_status_contexts": deleted_superseded_sc,
        "prs_invalidated_stale_pending_check_runs": len(prs_invalidated_stale_cr),
        "prs_invalidated_stale_pending_status_contexts": len(prs_invalidated_stale_sc),
        "prs_invalidated_superseded_check_runs": len(prs_invalidated_superseded_cr),
        "prs_invalidated_superseded_status_contexts": len(prs_invalidated_superseded_sc),
    }


@shared_task(name="syncer.expire_stale_ci_for_active_repos")
def expire_stale_ci_for_active_repos_task(  # type: ignore[no-redef]
    stale_pending_days: int | None = None,
) -> Dict[str, Any]:
    """Fan out expire_stale_ci_for_repo_task to all active repositories."""
    repos = list(Repository.objects.filter(is_active=True).only("id", "owner", "name"))
    for repo in repos:
        expire_stale_ci_for_repo_task.delay(repo.id, stale_pending_days=stale_pending_days)
    return {"repos": len(repos), "enqueued": len(repos)}


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


@shared_task(name="syncer.sync_label_catalog", bind=True)
def sync_label_catalog_task(  # type: ignore[no-redef]
    self,
    repo_id: int,
    *,
    page_size: int = 100,
) -> Dict[str, Any]:
    """Refresh ``LabelDef`` for one repo against GitHub's full label list.

    Fetches every page of ``repository.labels`` first, and only enters the
    upsert/delete transaction once pagination has completed successfully.
    A partial GraphQL response raises before any rows are deleted, so a
    transient API hiccup cannot cascade-blow-away ``PRLabel`` rows.
    """
    from syncer.services.sub.labels_sync import fetch_repo_label_catalog, sync_full_label_catalog

    repo = Repository.objects.get(id=int(repo_id))
    with repo_advisory_lock(repo.id) as acquired:
        if not acquired:
            log.info("sync_label_catalog: lock not acquired; skipping repo=%s/%s", repo.owner, repo.name)
            return {"skipped": True, "reason": "lock_not_acquired", "repo_id": repo.id}

        client = GitHubClient(operation="syncer_label_catalog", owner=repo.owner, repo=repo.name)

        def _fetch(after: Optional[str]) -> Dict[str, Any]:
            return client.get_repo_labels_page(owner=repo.owner, name=repo.name, first=int(page_size), after=after)

        nodes = fetch_repo_label_catalog(repo, _fetch)
        result = sync_full_label_catalog(repo, nodes)

    log.info(
        "sync_label_catalog: repo=%s/%s fetched=%s created=%s updated=%s deleted=%s",
        repo.owner,
        repo.name,
        len(nodes),
        result.created,
        result.updated,
        result.deleted,
    )
    return {
        "repo": f"{repo.owner}/{repo.name}",
        "repo_id": repo.id,
        "fetched": len(nodes),
        "created": result.created,
        "updated": result.updated,
        "deleted": result.deleted,
    }


@shared_task(name="syncer.sync_label_catalog_for_active_repos")
def sync_label_catalog_for_active_repos_task() -> Dict[str, Any]:
    """Fan out ``syncer.sync_label_catalog`` to every active repository."""
    repos = list(Repository.objects.filter(is_active=True).only("id", "owner", "name"))
    for repo in repos:
        sync_label_catalog_task.delay(repo.id)
    return {"repos": len(repos), "enqueued": len(repos)}
