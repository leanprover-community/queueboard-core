from __future__ import annotations

from celery import shared_task
from django.utils import timezone

from django.db.models import Q, Exists, OuterRef

from syncer.models import PullRequest, CommitHistoryHarvest, RepoBackfillCursor, SyncerConvergenceSnapshot
from core.models import Repository


@shared_task(name="syncer.collect_convergence")
def collect_syncer_convergence_task() -> dict:
    """Collect backfill/convergence counts per active repository."""
    collected_at = timezone.now()
    repos = list(Repository.objects.filter(is_active=True).only("id", "owner", "name"))
    rows = 0
    per_repo: list[dict] = []
    for repo in repos:
        qs = PullRequest.objects.filter(repository=repo)
        timeline_pending = qs.filter(timeline_backfill_done=False).count()
        commits_pending = qs.filter(commits_backfill_done=False).count()
        incomplete = qs.filter(Q(timeline_backfill_done=False) | Q(commits_backfill_done=False)).count()
        harvest_open = CommitHistoryHarvest.objects.filter(pull_request__repository=repo, has_more=True).count()
        cursor = RepoBackfillCursor.objects.filter(repository=repo).first()
        history_completed = bool(cursor.completed) if cursor else False

        SyncerConvergenceSnapshot.objects.create(
            repository=repo,
            collected_at=collected_at,
            timeline_backfill_pending=timeline_pending,
            commits_backfill_pending=commits_pending,
            incomplete_prs=incomplete,
            harvest_jobs_open=harvest_open,
            history_cursor_completed=history_completed,
        )
        rows += 1
        per_repo.append(
            {
                "repo": f"{repo.owner}/{repo.name}",
                "timeline_pending": timeline_pending,
                "commits_pending": commits_pending,
                "harvest_open": harvest_open,
                "history_completed": history_completed,
            }
        )
    return {"repos": len(repos), "rows_created": rows, "per_repo": per_repo}
