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
    max_pr_list = 10
    repos = list(Repository.objects.filter(is_active=True).only("id", "owner", "name"))
    total_rebuilt = 0
    total_prs_considered = 0
    total_prs_skipped_limit = 0
    per_repo: list[dict] = []
    processed_pr_numbers: list[int] = []
    for repo in repos:
        pr_qs = PullRequest.objects.filter(repository=repo, timeline_backfill_done=True).only(
            "id",
            "number",
            "timeline_backfill_done",
            "commits_backfill_done",
            "gh_updated_at",
            "gh_created_at",
        )
        if only_complete_backfill:
            pr_qs = pr_qs.filter(commits_backfill_done=True)
        pr_qs = pr_qs.order_by("-gh_updated_at", "-id").iterator(chunk_size=100)

        repo_rebuilt = 0
        repo_prs = 0
        repo_prs_skipped_limit: list[int] = []
        repo_prs_skipped_limit_count = 0
        repo_limit_hit = False
        for pr in pr_qs:
            if repo_prs >= int(max_prs_per_repo):
                repo_limit_hit = True
                repo_prs_skipped_limit_count += 1
                if len(repo_prs_skipped_limit) < max_pr_list:
                    repo_prs_skipped_limit.append(int(pr.number))
                break
            res = rebuild_pr_revisions(pr)
            if res.strategy != "noop":
                repo_rebuilt += 1
            repo_prs += 1
            total_prs_considered += 1
            if len(processed_pr_numbers) < max_pr_list:
                processed_pr_numbers.append(int(pr.number))
        total_rebuilt += repo_rebuilt
        total_prs_skipped_limit += repo_prs_skipped_limit_count
        per_repo.append(
            {
                "repo": f"{repo.owner}/{repo.name}",
                "prs_checked": repo_prs,
                "revisions_updated": repo_rebuilt,
                "prs_skipped_limit": repo_prs_skipped_limit,
                "limit_hit": repo_limit_hit,
            }
        )

    return {
        "repos": len(repos),
        "prs_checked": total_prs_considered,
        "prs_checked_numbers": processed_pr_numbers,
        "revisions_updated": total_rebuilt,
        "prs_skipped_limit": total_prs_skipped_limit,
        "only_complete_backfill": bool(only_complete_backfill),
        "per_repo": per_repo,
    }
