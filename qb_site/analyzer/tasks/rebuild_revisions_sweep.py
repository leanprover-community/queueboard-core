from __future__ import annotations

from celery import shared_task
from django.db.models import Exists, F, OuterRef, Q

from analyzer.services.revisions import PR_REVISION_BUILDER_VERSION, rebuild_pr_revisions
from analyzer.models import PRRevision, PRRevisionBuildState
from core.models import Repository
from syncer.models import PullRequest


@shared_task(name="analyzer.rebuild_revisions_sweep")
def rebuild_revisions_sweep_task(
    *,
    max_prs_per_repo: int = 50,
    only_complete_backfill: bool = False,
) -> dict:
    """Periodically rebuild PRRevision for eligible PRs across active repositories."""
    max_pr_list = 10
    repos = list(Repository.objects.filter(is_active=True).only("id", "owner", "name"))
    total_rebuilt = 0
    total_prs_considered = 0
    total_prs_skipped_limit = 0
    per_repo: list[dict] = []
    processed_pr_numbers: list[int] = []
    processed_pr_reasons: list[dict] = []
    total_created = 0
    total_deleted = 0
    strategy_counts = {"full": 0, "append": 0, "noop": 0, "skipped": 0}
    total_reason_counts = {
        "missing_build_state": 0,
        "dirty_from_ts": 0,
        "missing_last_built_at": 0,
        "updated_after_built_at": 0,
        "builder_version_mismatch": 0,
        "no_revisions": 0,
    }
    processed_updated_at_min = None
    processed_updated_at_max = None
    processed_id_min = None
    processed_id_max = None
    for repo in repos:
        has_revisions = PRRevision.objects.filter(pull_request=OuterRef("pk"))
        pr_qs = (
            PullRequest.objects.filter(repository=repo, timeline_backfill_done=True)
            .select_related("revision_build_state")
            .only(
                "id",
                "number",
                "timeline_backfill_done",
                "commits_backfill_done",
                "gh_updated_at",
                "gh_created_at",
                "revision_build_state__builder_version",
                "revision_build_state__dirty_from_ts",
                "revision_build_state__last_built_at",
            )
            .annotate(has_revisions=Exists(has_revisions))
        )
        if only_complete_backfill:
            pr_qs = pr_qs.filter(commits_backfill_done=True)

        needs_rebuild = (
            Q(revision_build_state__isnull=True)
            | Q(revision_build_state__dirty_from_ts__isnull=False)
            | Q(revision_build_state__last_built_at__isnull=True)
            | Q(gh_updated_at__gt=F("revision_build_state__last_built_at"))
            | ~Q(revision_build_state__builder_version=PR_REVISION_BUILDER_VERSION)
            | Q(has_revisions=False)
        )

        pr_qs = pr_qs.filter(needs_rebuild).order_by("-gh_updated_at", "-id").iterator(chunk_size=100)

        repo_rebuilt = 0
        repo_prs = 0
        repo_prs_skipped_limit: list[int] = []
        repo_prs_skipped_limit_count = 0
        repo_limit_hit = False
        repo_created = 0
        repo_deleted = 0
        repo_strategy_counts = {"full": 0, "append": 0, "noop": 0, "skipped": 0}
        repo_reason_counts = {
            "missing_build_state": 0,
            "dirty_from_ts": 0,
            "missing_last_built_at": 0,
            "updated_after_built_at": 0,
            "builder_version_mismatch": 0,
            "no_revisions": 0,
        }
        repo_updated_at_min = None
        repo_updated_at_max = None
        repo_id_min = None
        repo_id_max = None
        for pr in pr_qs:
            if repo_prs >= int(max_prs_per_repo):
                repo_limit_hit = True
                repo_prs_skipped_limit_count += 1
                if len(repo_prs_skipped_limit) < max_pr_list:
                    repo_prs_skipped_limit.append(int(pr.number))
                break
            try:
                state = pr.revision_build_state
            except PRRevisionBuildState.DoesNotExist:
                state = None
            reasons = {
                "missing_build_state": state is None,
                "dirty_from_ts": bool(getattr(state, "dirty_from_ts", None)),
                "missing_last_built_at": getattr(state, "last_built_at", None) is None,
                "updated_after_built_at": bool(
                    pr.gh_updated_at and getattr(state, "last_built_at", None) and pr.gh_updated_at > state.last_built_at
                ),
                "builder_version_mismatch": bool(state and state.builder_version != PR_REVISION_BUILDER_VERSION),
                "no_revisions": not bool(getattr(pr, "has_revisions", False)),
            }
            for key, enabled in reasons.items():
                if enabled:
                    repo_reason_counts[key] += 1
                    total_reason_counts[key] += 1

            res = rebuild_pr_revisions(pr)
            if res.strategy != "noop":
                repo_rebuilt += 1
            if res.strategy in repo_strategy_counts:
                repo_strategy_counts[res.strategy] += 1
                strategy_counts[res.strategy] += 1
            repo_prs += 1
            total_prs_considered += 1
            processed_pr_numbers.append(int(pr.number))
            if len(processed_pr_reasons) < 200:
                processed_pr_reasons.append(
                    {
                        "repo": f"{repo.owner}/{repo.name}",
                        "pr": int(pr.number),
                        "reasons": {k: v for k, v in reasons.items() if v},
                    }
                )
            repo_created += int(res.created)
            repo_deleted += int(res.deleted)
            total_created += int(res.created)
            total_deleted += int(res.deleted)
            if pr.gh_updated_at:
                if repo_updated_at_min is None or pr.gh_updated_at < repo_updated_at_min:
                    repo_updated_at_min = pr.gh_updated_at
                if repo_updated_at_max is None or pr.gh_updated_at > repo_updated_at_max:
                    repo_updated_at_max = pr.gh_updated_at
                if processed_updated_at_min is None or pr.gh_updated_at < processed_updated_at_min:
                    processed_updated_at_min = pr.gh_updated_at
                if processed_updated_at_max is None or pr.gh_updated_at > processed_updated_at_max:
                    processed_updated_at_max = pr.gh_updated_at
            if pr.id is not None:
                if repo_id_min is None or pr.id < repo_id_min:
                    repo_id_min = pr.id
                if repo_id_max is None or pr.id > repo_id_max:
                    repo_id_max = pr.id
                if processed_id_min is None or pr.id < processed_id_min:
                    processed_id_min = pr.id
                if processed_id_max is None or pr.id > processed_id_max:
                    processed_id_max = pr.id
        total_rebuilt += repo_rebuilt
        total_prs_skipped_limit += repo_prs_skipped_limit_count
        per_repo.append(
            {
                "repo": f"{repo.owner}/{repo.name}",
                "prs_checked": repo_prs,
                "revisions_updated": repo_rebuilt,
                "revisions_created": repo_created,
                "revisions_deleted": repo_deleted,
                "prs_skipped_limit": repo_prs_skipped_limit,
                "limit_hit": repo_limit_hit,
                "strategy_counts": repo_strategy_counts,
                "reason_counts": repo_reason_counts,
                "processed_updated_at_min": repo_updated_at_min,
                "processed_updated_at_max": repo_updated_at_max,
                "processed_id_min": repo_id_min,
                "processed_id_max": repo_id_max,
            }
        )

    return {
        "repos": len(repos),
        "prs_checked": total_prs_considered,
        "prs_checked_numbers": processed_pr_numbers,
        "prs_checked_reasons_sample": processed_pr_reasons,
        "revisions_updated": total_rebuilt,
        "revisions_created": total_created,
        "revisions_deleted": total_deleted,
        "prs_skipped_limit": total_prs_skipped_limit,
        "only_complete_backfill": bool(only_complete_backfill),
        "strategy_counts": strategy_counts,
        "reason_counts": total_reason_counts,
        "processed_updated_at_min": processed_updated_at_min,
        "processed_updated_at_max": processed_updated_at_max,
        "processed_id_min": processed_id_min,
        "processed_id_max": processed_id_max,
        "per_repo": per_repo,
    }
