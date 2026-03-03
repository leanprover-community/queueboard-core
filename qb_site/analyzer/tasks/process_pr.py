from __future__ import annotations

from django.db import transaction

from analyzer.services.revisions import rebuild_pr_revisions
from analyzer.services.queue_window_build_state import record_queue_window_build_states
from analyzer.services.queue_windows import queue_windows_need_rollup_backfill, rebuild_queue_windows_for_pr
from analyzer.models import QueueRuleSet
from syncer.services.github_client import GitHubClient
from syncer.models import PullRequest
from syncer.tasks.commit_history_tasks import harvest_commit_history_task
from analyzer.models import PRRevisionBuildState
from django.utils import timezone


def process_pr(
    pr: PullRequest,
    *,
    client: GitHubClient,
    harvest_max_pages: int = 1,
    harvest_page_size: int = 20,
    harvest_task: object | None = None,
) -> dict[str, str | int]:
    """Orchestrate revision rebuild and queue window rebuild for a single PR.

    This is a minimal orchestrator: it assumes timeline backfill is complete,
    runs the revision builder, and rebuilds queue windows for all rule sets when
    revisions changed. Tail-append handling is delegated to `rebuild_pr_revisions`.
    """
    if not getattr(pr, "timeline_backfill_done", False):
        return {"status": "skipped", "reason": "timeline_backfill_incomplete"}

    with transaction.atomic():
        res = rebuild_pr_revisions(pr)
        strategy = res.strategy

    harvest = {}
    ci_result: dict[str, object] = {
        "status": "skipped",
        "reason": "ci_planning_runs_in_process_pr_task",
        "planned": 0,
        "enqueued": [],
    }
    queue_results: dict[int, dict[str, object]] = {}
    rulesets = list(QueueRuleSet.objects.filter(repository=pr.repository, is_active=True))
    rollup_missing = False
    for rule_set in rulesets:
        if queue_windows_need_rollup_backfill(pr=pr, rule_set=rule_set):
            rollup_missing = True
            break

    if strategy != "noop" or rollup_missing:
        summary = rebuild_queue_windows_for_pr(pr=pr, rule_sets=rulesets)
        queue_results = summary.get("per_ruleset", {}) or {}
        # Harvest commit history per force-push segment baseline to surface missed heads via Syncer task.
        fps = list(
            pr.timeline_events.filter(type="HEAD_FORCE_PUSHED")
            .order_by("occurred_at", "id")
            .values_list("before_sha", "after_sha", "occurred_at")
        )
        segment_jobs: list[tuple[str, str | None]] = []
        prev_ts = pr.gh_created_at
        for before_sha, after_sha, occurred_at in fps:
            if before_sha:
                segment_jobs.append((before_sha, prev_ts.isoformat() if prev_ts else None))
            if after_sha and occurred_at:
                segment_jobs.append((after_sha, occurred_at.isoformat()))
            prev_ts = occurred_at or prev_ts
        task_fn = harvest_task or harvest_commit_history_task
        for sha, cutoff in segment_jobs:
            task_fn.delay(
                pr_id=pr.id,
                start_sha=sha,
                max_pages=harvest_max_pages,
                page_size=harvest_page_size,
                since_iso=cutoff,
            )
            # We fire-and-forget; Analyzer stays stateless for harvest. CI enqueue happens after we read results.
            # Results are returned by Celery; inlined polling could be added later if needed.
        # Nothing to enqueue yet without harvest results.
        harvest = {"harvested_shas": [], "tasks": len(segment_jobs)}

    if queue_results:
        state, _ = PRRevisionBuildState.objects.get_or_create(pull_request=pr)
        built_at = timezone.now()
        record_queue_window_build_states(
            pr=pr,
            rule_sets=rulesets,
            per_ruleset=queue_results,
            revision_version=int(state.revision_version),
            built_at=built_at,
        )

    return {
        "status": "ok",
        "revisions": strategy,
        "created": res.created,
        "deleted": res.deleted,
        "queue_windows": queue_results,
        "harvest": harvest,
        "ci_backfill": ci_result,
    }
