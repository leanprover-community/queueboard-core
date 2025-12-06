from __future__ import annotations

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from analyzer.models import PRRevision, PRRevisionBuildState
from analyzer.services.revisions import next_revision_backfill_shas
from analyzer.services.ci_backfill import enqueue_ci_by_shas
from core.models import Repository
from syncer.models import PullRequest


@shared_task(name="analyzer.plan_missing_ci")
def plan_missing_ci_backfill_task(
    *,
    max_prs_per_repo: int = 30,
    shas_per_pr: int = 2,
    pages_per_sha: int | None = None,
    only_complete_backfill: bool = False,
    require_pr_association: bool = False,
) -> dict:
    """Plan CI-by-SHA backfill for revision heads missing CI across active repositories.

    - Skips PRs without timeline backfill or without any PRRevision rows.
    - Skips PRs whose PRRevisionBuildState has ci_checked_revision_version matching revision_version.
    - Marks ci_checked_revision_version/ci_checked_at after checking a PR, even when no CI is enqueued,
      so we avoid repeatedly re-checking expired/missing CI until revisions change.
    """
    repos = list(Repository.objects.filter(is_active=True).only("id", "owner", "name"))
    total_enqueued = 0
    total_prs_considered = 0
    per_repo: list[dict] = []
    now_ts = timezone.now()
    for repo in repos:
        pr_qs = (
            PullRequest.objects.filter(repository=repo, timeline_backfill_done=True)
            .select_related("repository", "revision_build_state")
            .only(
                "id",
                "number",
                "repository",
                "repository__owner",
                "repository__name",
                "timeline_backfill_done",
                "commits_backfill_done",
                "gh_updated_at",
                "revision_build_state__revision_version",
                "revision_build_state__ci_checked_revision_version",
                "revision_build_state__ci_checked_at",
            )
        )
        if only_complete_backfill:
            pr_qs = pr_qs.filter(commits_backfill_done=True)
        pr_qs = pr_qs.order_by("-gh_updated_at", "-id").iterator(chunk_size=100)
        repo_enqueued = 0
        repo_prs = 0
        for pr in pr_qs:
            if repo_prs >= int(max_prs_per_repo):
                break

            state, _ = PRRevisionBuildState.objects.get_or_create(pull_request=pr)
            # Skip if already checked for this revision_version.
            if state.revision_version and state.ci_checked_revision_version == state.revision_version:
                continue
            if not PRRevision.objects.filter(pull_request=pr).exists():
                continue

            shas = next_revision_backfill_shas(pr, limit=int(shas_per_pr))
            if shas:
                enqueue_ci_by_shas(
                    pr=pr,
                    shas=shas,
                    pages_per_sha=int(pages_per_sha)
                    if pages_per_sha is not None
                    else int(getattr(settings, "SYNCER_CI_BY_SHA_PAGES", 1)),
                    require_pr_association=bool(require_pr_association),
                )
                repo_enqueued += 1
            # Mark that we have checked this revision_version (even if nothing was enqueued).
            state.ci_checked_revision_version = state.revision_version
            state.ci_checked_at = now_ts
            state.save(update_fields=["ci_checked_revision_version", "ci_checked_at", "updated_at"])

            repo_prs += 1
            total_prs_considered += 1

        total_enqueued += repo_enqueued
        per_repo.append({"repo": f"{repo.owner}/{repo.name}", "prs_checked": repo_prs, "ci_tasks": repo_enqueued})

    return {
        "repos": len(repos),
        "prs_checked": total_prs_considered,
        "ci_tasks": total_enqueued,
        "per_repo": per_repo,
        "only_complete_backfill": bool(only_complete_backfill),
    }
