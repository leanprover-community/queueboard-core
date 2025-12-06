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
    per_repo: list[dict] = []
    for repo in repos:
        pr_qs = (
            PullRequest.objects.filter(repository=repo, timeline_backfill_done=True)
            .only("id", "number", "timeline_backfill_done", "commits_backfill_done", "gh_updated_at", "gh_created_at")
            .order_by("-gh_updated_at", "-id")
            .iterator(chunk_size=100)
        )
        if only_complete_backfill:
            pr_qs = (p for p in pr_qs if p.commits_backfill_done)

        repo_rebuilt = 0
        repo_prs = 0
        for pr in pr_qs:
            if repo_prs >= int(max_prs_per_repo):
                break
            res = rebuild_pr_revisions(pr)
            if res.strategy != "noop":
                repo_rebuilt += 1
            repo_prs += 1
            total_prs_considered += 1
        total_rebuilt += repo_rebuilt
        per_repo.append({"repo": f"{repo.owner}/{repo.name}", "prs_checked": repo_prs, "revisions_updated": repo_rebuilt})

    return {
        "repos": len(repos),
        "prs_checked": total_prs_considered,
        "revisions_updated": total_rebuilt,
        "only_complete_backfill": bool(only_complete_backfill),
        "per_repo": per_repo,
    }
