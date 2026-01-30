from __future__ import annotations

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from analyzer.models import PRRevision, PRRevisionBuildState
from analyzer.services.revisions import next_revision_backfill_shas, revision_candidate_shas
from analyzer.services.ci_backfill import enqueue_ci_by_shas
from core.models import Repository
from syncer.models import CIShaFetchState, PullRequest


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
    - Uses CIShaFetchState terminal results (ok/empty/filtered/not_found) to avoid re-enqueueing
      revision heads that have already been checked to a terminal outcome.
    - Marks ci_checked_revision_version only when no actionable SHAs remain for the current
      revision_version; this is used as a skip gate for future sweeps.
    """
    repos = list(Repository.objects.filter(is_active=True).only("id", "owner", "name"))
    total_enqueued = 0
    total_prs_considered = 0
    total_prs_skipped_no_backfill = 0
    total_prs_skipped_no_revisions = 0
    total_prs_skipped_already_checked = 0
    total_prs_skipped_limit = 0
    total_prs_skipped_backoff = 0
    processed_pr_numbers: list[int] = []
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
        repo_prs_skipped_no_backfill: list[int] = []
        repo_prs_skipped_no_revisions: list[int] = []
        repo_prs_skipped_already_checked: list[int] = []
        repo_prs_skipped_limit: list[int] = []
        repo_prs_skipped_backoff: list[int] = []
        repo_limit_hit = False
        for pr in pr_qs:
            if repo_prs >= int(max_prs_per_repo):
                repo_limit_hit = True
                repo_prs_skipped_limit.append(int(pr.number))
                break

            if not getattr(pr, "timeline_backfill_done", False):
                repo_prs_skipped_no_backfill.append(int(pr.number))
                continue

            state, _ = PRRevisionBuildState.objects.get_or_create(pull_request=pr)
            # Skip if we already confirmed there were no actionable SHAs for this revision version.
            if state.revision_version and state.ci_checked_revision_version == state.revision_version:
                repo_prs_skipped_already_checked.append(int(pr.number))
                continue
            if not PRRevision.objects.filter(pull_request=pr).exists():
                repo_prs_skipped_no_revisions.append(int(pr.number))
                continue

            candidate_shas = revision_candidate_shas(pr)
            terminal_shas: set[str] = set()
            if candidate_shas:
                terminal_results = {"ok", "empty", "filtered", "not_found"}
                terminal_shas = set(
                    CIShaFetchState.objects.filter(
                        repository=repo,
                        sha__in=candidate_shas,
                        last_result__in=terminal_results,
                    ).values_list("sha", flat=True)
                )
            shas = next_revision_backfill_shas(pr, limit=int(shas_per_pr), skip_shas=terminal_shas)
            if shas:
                task_id = enqueue_ci_by_shas(
                    pr=pr,
                    shas=shas,
                    pages_per_sha=int(pages_per_sha)
                    if pages_per_sha is not None
                    else int(getattr(settings, "SYNCER_CI_BY_SHA_PAGES", 1)),
                    require_pr_association=bool(require_pr_association),
                )
                if task_id:
                    repo_enqueued += 1
                else:
                    repo_prs_skipped_backoff.append(int(pr.number))
            # Mark the revision version as checked only when there are no actionable SHAs.
            if not shas:
                state.ci_checked_revision_version = state.revision_version
            state.ci_checked_at = now_ts
            state.save(update_fields=["ci_checked_revision_version", "ci_checked_at", "updated_at"])

            repo_prs += 1
            total_prs_considered += 1
            processed_pr_numbers.append(int(pr.number))

        total_enqueued += repo_enqueued
        total_prs_skipped_no_backfill += len(repo_prs_skipped_no_backfill)
        total_prs_skipped_no_revisions += len(repo_prs_skipped_no_revisions)
        total_prs_skipped_already_checked += len(repo_prs_skipped_already_checked)
        total_prs_skipped_limit += len(repo_prs_skipped_limit)
        total_prs_skipped_backoff += len(repo_prs_skipped_backoff)
        per_repo.append(
            {
                "repo": f"{repo.owner}/{repo.name}",
                "prs_checked": repo_prs,
                "ci_tasks": repo_enqueued,
                "prs_skipped_no_backfill": repo_prs_skipped_no_backfill,
                "prs_skipped_no_revisions": repo_prs_skipped_no_revisions,
                "prs_skipped_already_checked": repo_prs_skipped_already_checked,
                "prs_skipped_limit": repo_prs_skipped_limit,
                "prs_skipped_backoff": repo_prs_skipped_backoff,
                "limit_hit": repo_limit_hit,
            }
        )

    return {
        "repos": len(repos),
        "prs_checked": total_prs_considered,
        "prs_checked_numbers": processed_pr_numbers,
        "ci_tasks": total_enqueued,
        "prs_skipped_no_backfill": total_prs_skipped_no_backfill,
        "prs_skipped_no_revisions": total_prs_skipped_no_revisions,
        "prs_skipped_already_checked": total_prs_skipped_already_checked,
        "prs_skipped_limit": total_prs_skipped_limit,
        "prs_skipped_backoff": total_prs_skipped_backoff,
        "per_repo": per_repo,
        "only_complete_backfill": bool(only_complete_backfill),
    }
