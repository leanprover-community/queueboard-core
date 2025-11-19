"""Background tasks for analytics refresh routines."""

from __future__ import annotations

import logging
from typing import Any, Dict

from celery import shared_task
from django.utils import timezone

from core.models import Repository
from syncer.models import PullRequest
from analyzer.models import QueueRuleSet
from analyzer.services.revisions import rebuild_pr_revisions
from analyzer.services.ci_backfill import plan_missing_ci_shas, enqueue_ci_by_shas
from analyzer.services.queue_windows import rebuild_queue_windows_for_ruleset


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

    # 1) Rebuild PRRevision windows when timeline history is fully backfilled.
    steps: Dict[str, Any] = {}
    if getattr(pr, "timeline_backfill_done", False):
        try:
            res = rebuild_pr_revisions(pr)
            steps["revisions"] = {"created": int(res.created), "deleted": int(res.deleted)}
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("analyzer.process_pr: rebuild_pr_revisions failed for PR id=%s", pr.id)
            steps["revisions"] = {"error": str(exc)}
    else:
        steps["revisions"] = {"skipped": True, "reason": "timeline_not_backfilled"}

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
            steps["ci_backfill"] = {"planned": len(plan), "enqueued": ci_enqueued}
        else:
            steps["ci_backfill"] = {"planned": 0, "enqueued": []}
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("analyzer.process_pr: CI backfill planning failed for PR id=%s", pr.id)
        steps["ci_backfill"] = {"error": str(exc)}

    # 3) Rebuild queue windows for all rulesets on this repository.
    qsteps: Dict[int, Dict[str, Any]] = {}
    for ruleset in QueueRuleSet.objects.filter(repository=repo):
        # Skip rulesets that are not intended to apply to this PR's creation
        # time, when effective bounds are configured.
        created_at = pr.gh_created_at
        if ruleset.effective_from and created_at < ruleset.effective_from:
            continue
        if ruleset.effective_to and created_at >= ruleset.effective_to:
            continue
        try:
            res = rebuild_queue_windows_for_ruleset(pr=pr, rule_set=ruleset, as_of=now)
            qsteps[int(ruleset.id)] = {
                "created": int(res.created),
                "updated": int(res.updated),
                "deleted": int(res.deleted),
                "require_ci_success": bool(ruleset.require_ci_success),
            }
        except Exception as exc:  # pragma: no cover - defensive
            log.exception(
                "analyzer.process_pr: rebuild_queue_windows_for_ruleset failed for PR id=%s ruleset_id=%s",
                pr.id,
                ruleset.id,
            )
            qsteps[int(ruleset.id)] = {"error": str(exc), "require_ci_success": bool(ruleset.require_ci_success)}

    steps["queue_windows"] = qsteps
    summary["steps"] = steps
    return summary


__all__ = ["process_pr_task"]
