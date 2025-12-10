from __future__ import annotations

from celery import shared_task
from django.conf import settings

from analyzer.services.revisions import rebuild_pr_revisions
from core.models import Repository
from syncer.models import PullRequest


@shared_task(name="analyzer.rebuild_revisions_sweep")
def rebuild_revisions_sweep_task(
    *,
    max_prs_per_repo: int = 50,
    only_complete_backfill: bool = False,
) -> dict:
    """Periodically rebuild PRRevision for eligible PRs across active repositories."""
    repos = list(Repository.objects.filter(is_active=True).only("id", "owner", "name"))
    total_rebuilt = 0
    total_prs_considered = 0
    total_prs_skipped_limit = 0
    total_prs_skipped_no_backfill = 0
    per_repo: list[dict] = []
    processed_pr_numbers: list[int] = []
    for repo in repos:
        pr_qs = (
            PullRequest.objects.filter(repository=repo)
            .only("id", "number", "timeline_backfill_done", "commits_backfill_done", "gh_updated_at", "gh_created_at")
            .order_by("-gh_updated_at", "-id")
            .iterator(chunk_size=100)
        )

        repo_rebuilt = 0
        repo_prs = 0
        repo_prs_skipped_limit: list[int] = []
        repo_prs_skipped_no_backfill: list[int] = []
        repo_prs_skipped_commits_backfill: list[int] = []
        repo_limit_hit = False
        for pr in pr_qs:
            if repo_prs >= int(max_prs_per_repo):
                repo_limit_hit = True
                repo_prs_skipped_limit.append(int(pr.number))
                break
            if not getattr(pr, "timeline_backfill_done", False):
                repo_prs_skipped_no_backfill.append(int(pr.number))
                continue
            if only_complete_backfill and not getattr(pr, "commits_backfill_done", False):
                repo_prs_skipped_commits_backfill.append(int(pr.number))
                continue
            res = rebuild_pr_revisions(pr)
            if res.strategy != "noop":
                repo_rebuilt += 1
            repo_prs += 1
            total_prs_considered += 1
            processed_pr_numbers.append(int(pr.number))
        total_rebuilt += repo_rebuilt
        total_prs_skipped_limit += len(repo_prs_skipped_limit)
        total_prs_skipped_no_backfill += len(repo_prs_skipped_no_backfill)
        per_repo.append(
            {
                "repo": f"{repo.owner}/{repo.name}",
                "prs_checked": repo_prs,
                "revisions_updated": repo_rebuilt,
                "prs_skipped_limit": repo_prs_skipped_limit,
                "prs_skipped_no_backfill": repo_prs_skipped_no_backfill,
                "prs_skipped_commits_backfill": repo_prs_skipped_commits_backfill,
                "limit_hit": repo_limit_hit,
            }
        )

    return {
        "repos": len(repos),
        "prs_checked": total_prs_considered,
        "prs_checked_numbers": processed_pr_numbers,
        "revisions_updated": total_rebuilt,
        "prs_skipped_limit": total_prs_skipped_limit,
        "prs_skipped_no_backfill": total_prs_skipped_no_backfill,
        "prs_skipped_commits_backfill": sum(len(repo["prs_skipped_commits_backfill"]) for repo in per_repo),
        "only_complete_backfill": bool(only_complete_backfill),
        "per_repo": per_repo,
    }
