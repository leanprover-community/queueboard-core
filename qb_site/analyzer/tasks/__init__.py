"""Background tasks for analytics refresh routines."""

from __future__ import annotations

import logging
from typing import Any, Dict

from celery import shared_task
from django.utils import timezone

from core.models import Repository
from syncer.models import PullRequest
from syncer.services.github_client import GitHubClient
from analyzer.models import QueueRuleSet, PRDependencyState, PRRevision
from analyzer.services.ci_backfill import plan_missing_ci_shas, enqueue_ci_by_shas
from analyzer.services.dependencies import rebuild_pr_dependencies, body_hash
from analyzer.tasks.process_pr import process_pr
from analyzer.tasks.dependencies import rebuild_pr_dependencies_task, rebuild_dependencies_sweep_task
from analyzer.tasks.plan_missing_ci import plan_missing_ci_backfill_task
from analyzer.tasks.rebuild_revisions_sweep import rebuild_revisions_sweep_task
from analyzer.tasks.rebuild_queue_windows_sweep import rebuild_queue_windows_sweep_task
from analyzer.tasks.collect_convergence import collect_analyzer_convergence_task
from analyzer.tasks.reviewer_assignment import (
    build_reviewer_assignment,
    refresh_reviewer_assignments_task,
    build_area_stats,
    refresh_area_stats_task,
)


log = logging.getLogger(__name__)


@shared_task(name="analyzer.process_pr")
def process_pr_task(pr_id: int) -> Dict[str, Any]:
    """Process a single PR in Analyzer after Syncer has synced it.

    Behavior
    - Optionally rebuilds PRRevision windows when timeline backfill is complete.
    - Plans CI-by-SHA backfill for missing revision heads using Analyzer helpers.
    - Rebuilds PRQueueWindow rows for all QueueRuleSet rows on the PR's repository,
      respecting gating conditions so that windows are only persisted when the
      underlying data is complete enough for the analyzed horizon.
    """
    pr = PullRequest.objects.select_related("repository").filter(id=int(pr_id)).first()
    if pr is None:
        log.info("analyzer.process_pr: PR not found id=%s", pr_id)
        return {"skipped": True, "reason": "pr_not_found"}

    repo: Repository = pr.repository
    now = timezone.now()

    summary: Dict[str, Any] = {
        "skipped": False,
        "repo": f"{repo.owner}/{repo.name}",
        "repo_id": repo.id,
        "number": int(pr.number),
        "timeline_backfill_done": bool(getattr(pr, "timeline_backfill_done", False)),
        "steps": {},
    }

    steps: Dict[str, Any] = {}
    # 0) Parse PR body dependencies.
    try:
        deps_res = rebuild_pr_dependencies(pr)
        steps["dependencies"] = {
            "created": int(deps_res.created),
            "updated": int(deps_res.updated),
            "deleted": int(deps_res.deleted),
            "parsed_numbers": deps_res.parsed_numbers,
            "resolved_numbers": deps_res.resolved_numbers,
            "unresolved_numbers": deps_res.unresolved_numbers,
        }
        dep_state, _ = PRDependencyState.objects.get_or_create(pull_request=pr)
        dep_state.last_checked_at = now
        dep_state.last_body_hash = body_hash(pr.body)
        dep_state.builder_version = dep_state.builder_version or 1
        dep_state.save(update_fields=["last_checked_at", "last_body_hash", "builder_version", "updated_at"])
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("analyzer.process_pr: dependency rebuild failed for PR id=%s", pr.id)
        steps["dependencies"] = {"error": str(exc)}

    # 1) Run the orchestrator (revisions + queue windows) when timeline is backfilled.
    if getattr(pr, "timeline_backfill_done", False):
        try:
            gh_client = GitHubClient()
            proc_res = process_pr(pr, client=gh_client)
            steps["revisions"] = {
                "created": int(proc_res.get("created", 0)),
                "deleted": int(proc_res.get("deleted", 0)),
                "strategy": proc_res.get("revisions"),
            }
            steps["queue_windows"] = proc_res.get("queue_windows", {})
            steps["harvest"] = proc_res.get("harvest", {})
            steps["ci_backfill"] = proc_res.get("ci_backfill", [])
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("analyzer.process_pr: orchestrator failed for PR id=%s", pr.id)
            steps["revisions"] = {"error": str(exc)}
            steps["queue_windows"] = {"error": str(exc)}
            steps["harvest"] = {"error": str(exc)}
            steps["ci_backfill"] = {"error": str(exc)}
    else:
        steps["revisions"] = {"skipped": True, "reason": "timeline_not_backfilled"}
        steps["queue_windows"] = {"skipped": True, "reason": "timeline_not_backfilled"}

    # 2) Plan CI-by-SHA backfill for missing revision heads (small per-PR budget).
    try:
        plan = plan_missing_ci_shas(repo=repo, pr_numbers=[pr.number], limit_per_pr=2)
        if plan:
            # For now, always enqueue using existing Analyzer helper.
            # This is rate-aware and leverages Syncer's sync_ci_for_shas task.
            ci_enqueued = []
            for item in plan:
                task_id = enqueue_ci_by_shas(
                    pr=item.pr,
                    shas=item.shas,
                    pages_per_sha=1,
                    require_pr_association=False,
                )
                ci_enqueued.append({"pr_number": int(item.pr.number), "shas": list(item.shas), "task_id": task_id})
            steps["ci_backfill"] = {"planned": len(plan), "enqueued": ci_enqueued, "status": "enqueued"}
        else:
            has_revisions = PRRevision.objects.filter(pull_request=pr).exists()
            reason = "no_pr_revisions" if not has_revisions else "no_missing_ci_shas"
            steps["ci_backfill"] = {"planned": 0, "enqueued": [], "status": "skipped", "reason": reason}
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("analyzer.process_pr: CI backfill planning failed for PR id=%s", pr.id)
        steps["ci_backfill"] = {"error": str(exc), "status": "error"}

    summary["steps"] = steps
    return summary


__all__ = [
    "process_pr_task",
    "plan_missing_ci_backfill_task",
    "rebuild_revisions_sweep_task",
    "rebuild_queue_windows_sweep_task",
    "collect_analyzer_convergence_task",
    "rebuild_pr_dependencies_task",
    "rebuild_dependencies_sweep_task",
    "build_reviewer_assignment",
    "refresh_reviewer_assignments_task",
    "build_area_stats",
    "refresh_area_stats_task",
]
