from __future__ import annotations

from celery import shared_task
from django.conf import settings
from django.db.models import Exists, F, OuterRef, Q
from django.utils import timezone

from analyzer.models import PRRevision, PRRevisionBuildState
from analyzer.services.revisions import next_revision_backfill_shas, revision_candidate_shas
from analyzer.services.ci_backfill import enqueue_ci_by_shas
from core.models import Repository
from syncer.models import CIShaFetchState, PullRequest
from syncer.services.ci_backoff import should_enqueue_ci_sha


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
    max_pr_list = 10
    repos = list(Repository.objects.filter(is_active=True).only("id", "owner", "name"))
    total_enqueued = 0
    total_prs_considered = 0
    total_prs_skipped_limit = 0
    total_prs_skipped_backoff = 0
    processed_pr_numbers: list[int] = []
    per_repo: list[dict] = []
    debug_prs: list[dict] = []
    now_ts = timezone.now()
    for repo in repos:
        has_revisions = PRRevision.objects.filter(pull_request=OuterRef("pk"))
        pr_qs_base = (
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
            .annotate(has_revisions=Exists(has_revisions))
            .filter(has_revisions=True)
        )
        if only_complete_backfill:
            pr_qs_base = pr_qs_base.filter(commits_backfill_done=True)

        already_checked_filter = Q(revision_build_state__revision_version__gt=0) & Q(
            revision_build_state__ci_checked_revision_version=F("revision_build_state__revision_version")
        )

        pr_qs_all = pr_qs_base.exclude(already_checked_filter)
        error_shas = CIShaFetchState.objects.filter(repository=repo, last_result="error").values("sha")
        error_prs = pr_qs_all.annotate(
            has_error_shas=Exists(PRRevision.objects.filter(pull_request=OuterRef("pk"), head_sha__in=error_shas))
        ).filter(has_error_shas=True)
        repo_enqueued = 0
        repo_prs = 0
        repo_prs_skipped_limit: list[int] = []
        repo_prs_skipped_backoff: list[int] = []
        repo_debug_prs: list[dict] = []
        repo_prs_skipped_limit_count = 0
        repo_prs_skipped_backoff_count = 0
        repo_limit_hit = False
        seen_pr_ids: set[int] = set()
        for pr in error_prs.order_by("-gh_updated_at", "-id").iterator(chunk_size=100):
            if repo_prs >= int(max_prs_per_repo):
                repo_limit_hit = True
                repo_prs_skipped_limit_count += 1
                if len(repo_prs_skipped_limit) < max_pr_list:
                    repo_prs_skipped_limit.append(int(pr.number))
                break
            seen_pr_ids.add(int(pr.id))
            state, _ = PRRevisionBuildState.objects.get_or_create(pull_request=pr)
            candidate_shas = revision_candidate_shas(pr)
            terminal_shas: set[str] = set()
            error_shas: set[str] = set()
            if candidate_shas:
                terminal_results = {"ok", "empty", "filtered", "not_found"}
                terminal_shas = set(
                    CIShaFetchState.objects.filter(
                        repository=repo,
                        sha__in=candidate_shas,
                        last_result__in=terminal_results,
                    ).values_list("sha", flat=True)
                )
                error_shas = set(
                    CIShaFetchState.objects.filter(
                        repository=repo,
                        sha__in=candidate_shas,
                        last_result="error",
                    ).values_list("sha", flat=True)
                )
            if error_shas:
                prioritized = [sha for sha in candidate_shas if sha in error_shas] + [
                    sha for sha in candidate_shas if sha not in error_shas
                ]
            else:
                prioritized = None
            shas = next_revision_backfill_shas(
                pr,
                limit=int(shas_per_pr),
                skip_shas=terminal_shas,
                candidates_override=prioritized,
            )
            if not shas and error_shas:
                shas = [sha for sha in candidate_shas if sha in error_shas][: int(shas_per_pr)]
            actionable_shas = [sha for sha in shas if should_enqueue_ci_sha(pr=pr, sha=sha, reason="analyzer.plan_missing_ci")]
            backoff_shas = [sha for sha in shas if sha not in actionable_shas]
            if actionable_shas:
                task_id = enqueue_ci_by_shas(
                    pr=pr,
                    shas=actionable_shas,
                    pages_per_sha=int(pages_per_sha)
                    if pages_per_sha is not None
                    else int(getattr(settings, "SYNCER_CI_BY_SHA_PAGES", 1)),
                    require_pr_association=bool(require_pr_association),
                )
                if task_id:
                    repo_enqueued += 1
                else:
                    repo_prs_skipped_backoff_count += 1
                    if len(repo_prs_skipped_backoff) < max_pr_list:
                        repo_prs_skipped_backoff.append(int(pr.number))
            elif shas:
                repo_prs_skipped_backoff_count += 1
                if len(repo_prs_skipped_backoff) < max_pr_list:
                    repo_prs_skipped_backoff.append(int(pr.number))
            # Mark checked only when there are no planned SHAs at all; backoff-blocked SHAs
            # should retry in a later sweep.
            if not shas:
                state.ci_checked_revision_version = state.revision_version
            state.ci_checked_at = now_ts
            state.save(update_fields=["ci_checked_revision_version", "ci_checked_at", "updated_at"])

            if actionable_shas:
                repo_prs += 1
                total_prs_considered += 1
                processed_pr_numbers.append(int(pr.number))
                repo_debug_prs.append(
                    {
                        "number": int(pr.number),
                        "revision_version": int(state.revision_version or 0),
                        "ci_checked_revision_version": int(state.ci_checked_revision_version or 0)
                        if state.ci_checked_revision_version is not None
                        else None,
                        "ci_checked_at": state.ci_checked_at.isoformat() if state.ci_checked_at else None,
                        "windows_built_revision_version": int(state.windows_built_revision_version or 0)
                        if state.windows_built_revision_version is not None
                        else None,
                        "windows_built_at": state.windows_built_at.isoformat() if state.windows_built_at else None,
                        "state_updated_at": state.updated_at.isoformat() if state.updated_at else None,
                        "candidate_count": len(candidate_shas),
                        "terminal_shas": list(terminal_shas),
                        "error_shas": list(error_shas),
                        "planned_shas": list(shas),
                        "actionable_shas": actionable_shas,
                        "backoff_shas": backoff_shas,
                    }
                )

        if repo_prs < int(max_prs_per_repo):
            pr_qs = pr_qs_all.exclude(id__in=seen_pr_ids).order_by("-gh_updated_at", "-id").iterator(chunk_size=100)
        else:
            pr_qs = []
        for pr in pr_qs:
            if repo_prs >= int(max_prs_per_repo):
                repo_limit_hit = True
                repo_prs_skipped_limit_count += 1
                if len(repo_prs_skipped_limit) < max_pr_list:
                    repo_prs_skipped_limit.append(int(pr.number))
                break

            state, _ = PRRevisionBuildState.objects.get_or_create(pull_request=pr)
            candidate_shas = revision_candidate_shas(pr)
            terminal_shas: set[str] = set()
            error_shas: set[str] = set()
            if candidate_shas:
                terminal_results = {"ok", "empty", "filtered", "not_found"}
                terminal_shas = set(
                    CIShaFetchState.objects.filter(
                        repository=repo,
                        sha__in=candidate_shas,
                        last_result__in=terminal_results,
                    ).values_list("sha", flat=True)
                )
                error_shas = set(
                    CIShaFetchState.objects.filter(
                        repository=repo,
                        sha__in=candidate_shas,
                        last_result="error",
                    ).values_list("sha", flat=True)
                )
            if error_shas:
                prioritized = [sha for sha in candidate_shas if sha in error_shas] + [
                    sha for sha in candidate_shas if sha not in error_shas
                ]
            else:
                prioritized = None
            shas = next_revision_backfill_shas(
                pr,
                limit=int(shas_per_pr),
                skip_shas=terminal_shas,
                candidates_override=prioritized,
            )
            if not shas and error_shas:
                shas = [sha for sha in candidate_shas if sha in error_shas][: int(shas_per_pr)]
            actionable_shas = [sha for sha in shas if should_enqueue_ci_sha(pr=pr, sha=sha, reason="analyzer.plan_missing_ci")]
            backoff_shas = [sha for sha in shas if sha not in actionable_shas]
            if actionable_shas:
                task_id = enqueue_ci_by_shas(
                    pr=pr,
                    shas=actionable_shas,
                    pages_per_sha=int(pages_per_sha)
                    if pages_per_sha is not None
                    else int(getattr(settings, "SYNCER_CI_BY_SHA_PAGES", 1)),
                    require_pr_association=bool(require_pr_association),
                )
                if task_id:
                    repo_enqueued += 1
                else:
                    repo_prs_skipped_backoff_count += 1
                    if len(repo_prs_skipped_backoff) < max_pr_list:
                        repo_prs_skipped_backoff.append(int(pr.number))
            elif shas:
                repo_prs_skipped_backoff_count += 1
                if len(repo_prs_skipped_backoff) < max_pr_list:
                    repo_prs_skipped_backoff.append(int(pr.number))
            # Mark checked only when there are no planned SHAs at all; backoff-blocked SHAs
            # should retry in a later sweep.
            if not shas:
                state.ci_checked_revision_version = state.revision_version
            state.ci_checked_at = now_ts
            state.save(update_fields=["ci_checked_revision_version", "ci_checked_at", "updated_at"])

            if actionable_shas:
                repo_prs += 1
                total_prs_considered += 1
                processed_pr_numbers.append(int(pr.number))
                repo_debug_prs.append(
                    {
                        "number": int(pr.number),
                        "revision_version": int(state.revision_version or 0),
                        "ci_checked_revision_version": int(state.ci_checked_revision_version or 0)
                        if state.ci_checked_revision_version is not None
                        else None,
                        "ci_checked_at": state.ci_checked_at.isoformat() if state.ci_checked_at else None,
                        "windows_built_revision_version": int(state.windows_built_revision_version or 0)
                        if state.windows_built_revision_version is not None
                        else None,
                        "windows_built_at": state.windows_built_at.isoformat() if state.windows_built_at else None,
                        "state_updated_at": state.updated_at.isoformat() if state.updated_at else None,
                        "candidate_count": len(candidate_shas),
                        "terminal_shas": list(terminal_shas),
                        "error_shas": list(error_shas),
                        "planned_shas": list(shas),
                        "actionable_shas": actionable_shas,
                        "backoff_shas": backoff_shas,
                    }
                )

        total_enqueued += repo_enqueued
        total_prs_skipped_limit += repo_prs_skipped_limit_count
        total_prs_skipped_backoff += repo_prs_skipped_backoff_count
        per_repo.append(
            {
                "repo": f"{repo.owner}/{repo.name}",
                "prs_checked": repo_prs,
                "ci_tasks": repo_enqueued,
                "prs_skipped_limit": repo_prs_skipped_limit,
                "prs_skipped_backoff": repo_prs_skipped_backoff,
                "limit_hit": repo_limit_hit,
            }
        )
        debug_prs.extend(repo_debug_prs)

    return {
        "repos": len(repos),
        "prs_checked": total_prs_considered,
        "prs_checked_numbers": processed_pr_numbers,
        "ci_tasks": total_enqueued,
        "prs_skipped_limit": total_prs_skipped_limit,
        "prs_skipped_backoff": total_prs_skipped_backoff,
        "per_repo": per_repo,
        "debug_prs": debug_prs,
        "only_complete_backfill": bool(only_complete_backfill),
    }
