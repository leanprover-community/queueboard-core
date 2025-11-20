from __future__ import annotations

from django.db import transaction

from analyzer.services.revisions import rebuild_pr_revisions
from analyzer.services.queue_windows import rebuild_queue_windows_for_ruleset
from analyzer.models import QueueRuleSet
from analyzer.services.ci_backfill import enqueue_ci_by_shas
from syncer.services.github_client import GitHubClient
from syncer.services.commit_history import harvest_commit_history_shas
from syncer.models import PullRequest


def process_pr(
    pr: PullRequest,
    *,
    client: GitHubClient,
    harvest_max_pages: int = 1,
    harvest_page_size: int = 20,
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
    ci_enqueued: list[dict[str, object]] = []
    queue_results: dict[int, dict[str, int]] = {}
    if strategy != "noop":
        for rule_set in QueueRuleSet.objects.filter(repository=pr.repository):
            created_at = pr.gh_created_at
            if rule_set.effective_from and created_at < rule_set.effective_from:
                continue
            if rule_set.effective_to and created_at >= rule_set.effective_to:
                continue
            rebuild = rebuild_queue_windows_for_ruleset(pr, rule_set)
            queue_results[int(rule_set.id)] = {
                "created": int(rebuild.created),
                "updated": int(rebuild.updated),
                "deleted": int(rebuild.deleted),
            }
        # Harvest commit history per force-push segment baseline to surface missed heads.
        fps = list(
            pr.timeline_events.filter(type="HEAD_FORCE_PUSHED")
            .order_by("occurred_at", "id")
            .values_list("before_sha", "after_sha", "occurred_at")
        )
        segment_heads = set()
        for before_sha, after_sha, _ in fps:
            for sha in (before_sha, after_sha):
                if sha:
                    segment_heads.add(sha)
        harvested: set[str] = set()
        for sha in segment_heads:
            shas = harvest_commit_history_shas(
                client=client,
                repo=pr.repository,
                start_sha=sha,
                max_pages=harvest_max_pages,
                page_size=harvest_page_size,
            )
            for s in shas:
                harvested.add(s)
        harvest_list = list(harvested)
        harvest = {"harvested_shas": harvest_list}
        # Enqueue CI backfill for harvested heads that lack CI.
        missing_shas = []
        for sha in harvest_list:
            has_cr = pr.check_runs.filter(head_sha=sha).exists()
            has_sc = pr.status_contexts.filter(head_sha=sha).exists()
            if not (has_cr or has_sc):
                missing_shas.append(sha)
        if missing_shas:
            task_id = enqueue_ci_by_shas(
                pr=pr,
                shas=missing_shas,
                pages_per_sha=1,
                require_pr_association=False,
            )
            ci_enqueued.append({"task_id": task_id, "shas": missing_shas})

    return {
        "status": "ok",
        "revisions": strategy,
        "created": res.created,
        "deleted": res.deleted,
        "queue_windows": queue_results,
        "harvest": harvest,
        "ci_backfill": ci_enqueued,
    }
