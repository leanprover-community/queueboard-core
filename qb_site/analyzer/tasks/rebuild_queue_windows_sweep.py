from __future__ import annotations

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from django.db.models import Exists, OuterRef

from analyzer.models import QueueRuleSet, PRRevisionBuildState, PRRevision
from analyzer.services.queue_windows import queue_windows_need_rollup_backfill, rebuild_queue_windows_for_pr
from core.models import Repository
from syncer.models import PullRequest


@shared_task(name="analyzer.rebuild_queue_windows_sweep")
def rebuild_queue_windows_sweep_task(
    *,
    max_prs_per_repo: int = 50,
    only_complete_backfill: bool = False,
) -> dict:
    """Rebuild queue windows for PRs whose revision_version changed or windows are stale."""
    max_pr_list = 10
    now_ts = timezone.now()
    repos = list(Repository.objects.filter(is_active=True).only("id", "owner", "name"))
    total_rebuilt = 0
    total_prs = 0
    total_prs_skipped_up_to_date = 0
    total_rulesets_skipped_out_of_bounds = 0
    total_prs_stale_ruleset = 0
    total_prs_rebuilt_stale_ruleset = 0
    processed_pr_numbers: list[int] = []
    per_repo: list[dict] = []

    for repo in repos:
        has_revisions = PRRevision.objects.filter(pull_request=OuterRef("pk"))
        pr_qs = (
            PullRequest.objects.filter(repository=repo, timeline_backfill_done=True)
            .select_related("revision_build_state")
            .only(
                "id",
                "number",
                "gh_created_at",
                "gh_updated_at",
                "timeline_backfill_done",
                "commits_backfill_done",
                "revision_build_state__revision_version",
                "revision_build_state__windows_built_revision_version",
                "revision_build_state__windows_built_at",
            )
            .annotate(has_revisions=Exists(has_revisions))
            .filter(has_revisions=True)
            .order_by("-gh_updated_at", "-id")
            .iterator(chunk_size=100)
        )
        if only_complete_backfill:
            pr_qs = (
                PullRequest.objects.filter(
                    repository=repo,
                    timeline_backfill_done=True,
                    commits_backfill_done=True,
                )
                .select_related("revision_build_state")
                .only(
                    "id",
                    "number",
                    "gh_created_at",
                    "gh_updated_at",
                    "timeline_backfill_done",
                    "commits_backfill_done",
                    "revision_build_state__revision_version",
                    "revision_build_state__windows_built_revision_version",
                    "revision_build_state__windows_built_at",
                )
                .annotate(has_revisions=Exists(has_revisions))
                .filter(has_revisions=True)
                .order_by("-gh_updated_at", "-id")
                .iterator(chunk_size=100)
            )
        repo_rebuilt = 0
        repo_prs = 0
        repo_prs_skipped_up_to_date: list[int] = []
        repo_rulesets_skipped_out_of_bounds: list[int] = []
        repo_prs_stale_ruleset: list[int] = []
        repo_prs_rebuilt_stale_ruleset: list[int] = []
        repo_prs_skipped_up_to_date_seen: set[int] = set()
        repo_rulesets_skipped_out_of_bounds_seen: set[int] = set()
        repo_prs_stale_ruleset_seen: set[int] = set()
        repo_prs_rebuilt_stale_ruleset_seen: set[int] = set()
        repo_limit_hit = False

        rulesets = list(QueueRuleSet.objects.filter(repository=repo, is_active=True))
        if not rulesets:
            per_repo.append(
                {
                    "repo": f"{repo.owner}/{repo.name}",
                    "prs_checked": 0,
                    "windows_rebuilt": 0,
                    "prs_skipped_up_to_date": 0,
                    "rulesets_skipped_out_of_bounds": 0,
                    "prs_stale_ruleset": 0,
                    "prs_rebuilt_stale_ruleset": 0,
                    "limit_hit": False,
                }
            )
            continue

        for pr in pr_qs:
            if repo_prs >= int(max_prs_per_repo):
                repo_limit_hit = True
                break

            state, _ = PRRevisionBuildState.objects.get_or_create(pull_request=pr)
            # Skip if windows already built for current revision_version and not stale vs ruleset updates.
            stale_ruleset = False
            for rs in rulesets:
                if state.windows_built_at and rs.updated_at and state.windows_built_at < rs.updated_at:
                    stale_ruleset = True
                    break
                if queue_windows_need_rollup_backfill(pr=pr, rule_set=rs):
                    stale_ruleset = True
                    break
            if stale_ruleset:
                pr_num = int(pr.number)
                if pr_num not in repo_prs_stale_ruleset_seen:
                    repo_prs_stale_ruleset_seen.add(pr_num)
                    if len(repo_prs_stale_ruleset) < max_pr_list:
                        repo_prs_stale_ruleset.append(pr_num)
            if (
                state.windows_built_revision_version is not None
                and state.windows_built_revision_version == state.revision_version
                and not stale_ruleset
            ):
                pr_num = int(pr.number)
                if pr_num not in repo_prs_skipped_up_to_date_seen:
                    repo_prs_skipped_up_to_date_seen.add(pr_num)
                    if len(repo_prs_skipped_up_to_date) < max_pr_list:
                        repo_prs_skipped_up_to_date.append(pr_num)
                continue

            summary = rebuild_queue_windows_for_pr(pr=pr, rule_sets=rulesets)
            per_ruleset = summary.get("per_ruleset", {}) or {}
            pr_num = int(pr.number)
            if any(
                res.get("reason") in {"pr_before_ruleset_effective_from", "pr_on_or_after_ruleset_effective_to"}
                for res in per_ruleset.values()
                if isinstance(res, dict)
            ):
                if pr_num not in repo_rulesets_skipped_out_of_bounds_seen:
                    repo_rulesets_skipped_out_of_bounds_seen.add(pr_num)
                    if len(repo_rulesets_skipped_out_of_bounds) < max_pr_list:
                        repo_rulesets_skipped_out_of_bounds.append(pr_num)
            rebuilt_any = bool(
                int(summary.get("created", 0) or 0) or int(summary.get("updated", 0) or 0) or int(summary.get("deleted", 0) or 0)
            )
            if stale_ruleset:
                if rebuilt_any and pr_num not in repo_prs_rebuilt_stale_ruleset_seen:
                    repo_prs_rebuilt_stale_ruleset_seen.add(pr_num)
                    if len(repo_prs_rebuilt_stale_ruleset) < max_pr_list:
                        repo_prs_rebuilt_stale_ruleset.append(pr_num)
            # Mark windows as rebuilt for the current revision even if the rebuild is a no-op.
            # This prevents endless rechecks after ruleset timestamp bumps.
            state.windows_built_revision_version = state.revision_version
            state.windows_built_at = now_ts
            state.save(update_fields=["windows_built_revision_version", "windows_built_at", "updated_at"])
            if rebuilt_any:
                repo_rebuilt += 1

            repo_prs += 1
            total_prs += 1
            if len(processed_pr_numbers) < max_pr_list:
                processed_pr_numbers.append(int(pr.number))
        total_rebuilt += repo_rebuilt
        total_prs_skipped_up_to_date += len(repo_prs_skipped_up_to_date_seen)
        total_rulesets_skipped_out_of_bounds += len(repo_rulesets_skipped_out_of_bounds_seen)
        total_prs_stale_ruleset += len(repo_prs_stale_ruleset_seen)
        total_prs_rebuilt_stale_ruleset += len(repo_prs_rebuilt_stale_ruleset_seen)
        per_repo.append(
            {
                "repo": f"{repo.owner}/{repo.name}",
                "prs_checked": repo_prs,
                "windows_rebuilt": repo_rebuilt,
                "prs_skipped_up_to_date": repo_prs_skipped_up_to_date,
                "rulesets_skipped_out_of_bounds": repo_rulesets_skipped_out_of_bounds,
                "prs_stale_ruleset": repo_prs_stale_ruleset,
                "prs_rebuilt_stale_ruleset": repo_prs_rebuilt_stale_ruleset,
                "limit_hit": repo_limit_hit,
            }
        )

    return {
        "repos": len(repos),
        "prs_checked": total_prs,
        "prs_checked_numbers": processed_pr_numbers,
        "windows_rebuilt": total_rebuilt,
        "prs_skipped_up_to_date": total_prs_skipped_up_to_date,
        "rulesets_skipped_out_of_bounds": total_rulesets_skipped_out_of_bounds,
        "prs_stale_ruleset": total_prs_stale_ruleset,
        "prs_rebuilt_stale_ruleset": total_prs_rebuilt_stale_ruleset,
        "only_complete_backfill": bool(only_complete_backfill),
        "per_repo": per_repo,
    }
