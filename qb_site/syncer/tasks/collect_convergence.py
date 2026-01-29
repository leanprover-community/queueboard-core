from __future__ import annotations

from celery import shared_task
from django.conf import settings
from django.db.models import DateTimeField, ExpressionWrapper, F, Q, Exists, OuterRef
from django.utils import timezone

from syncer.models import (
    PullRequest,
    CommitHistoryHarvest,
    RepoBackfillCursor,
    SyncerConvergenceSnapshot,
    CheckRun,
    StatusContext,
)
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
        eps = int(getattr(settings, "SYNCER_LAST_SYNC_EPSILON_SECONDS", 300))
        stale_cutoff = ExpressionWrapper(
            F("gh_updated_at") - timezone.timedelta(seconds=max(0, eps)),
            output_field=DateTimeField(),
        )
        incomplete = qs.filter(
            Q(timeline_backfill_done=False)
            | Q(commits_backfill_done=False)
            | Q(last_synced_at__isnull=True)
            | Q(last_synced_at__lt=stale_cutoff)
            | Q(head_ci_state__iexact="PENDING")
        ).count()
        harvest_open = CommitHistoryHarvest.objects.filter(pull_request__repository=repo, has_more=True).count()
        cursor = RepoBackfillCursor.objects.filter(repository=repo).first()
        history_completed = bool(cursor.completed) if cursor else False

        engagement_missing = qs.filter(engagement_synced_at__isnull=True).count()
        engagement_incomplete = (
            qs.filter(
                Q(files_incomplete=True) | Q(assignees_incomplete=True) | Q(reviews_incomplete=True) | Q(comments_incomplete=True)
            )
            .distinct()
            .count()
        )
        missing_head_ci = qs.filter(head_ci_state__isnull=True).count()
        missing_head_sha = qs.filter(Q(head_sha__isnull=True) | Q(head_sha="")).count()
        head_cr = CheckRun.objects.filter(pull_request=OuterRef("pk"), head_sha=OuterRef("head_sha"))
        head_sc = StatusContext.objects.filter(pull_request=OuterRef("pk"), head_sha=OuterRef("head_sha"))
        missing_head_contexts = (
            qs.filter(state="open")
            .filter(head_sha__isnull=False)
            .exclude(head_sha="")
            .annotate(has_head_cr=Exists(head_cr), has_head_sc=Exists(head_sc))
            .filter(has_head_cr=False, has_head_sc=False)
            .count()
        )

        SyncerConvergenceSnapshot.objects.create(
            repository=repo,
            collected_at=collected_at,
            timeline_backfill_pending=timeline_pending,
            commits_backfill_pending=commits_pending,
            incomplete_prs=incomplete,
            harvest_jobs_open=harvest_open,
            history_cursor_completed=history_completed,
            prs_missing_engagement=engagement_missing,
            prs_engagement_incomplete=engagement_incomplete,
            prs_missing_head_ci_state=missing_head_ci,
            prs_missing_head_sha=missing_head_sha,
            prs_missing_head_ci_contexts=missing_head_contexts,
        )
        rows += 1
        per_repo.append(
            {
                "repo": f"{repo.owner}/{repo.name}",
                "timeline_pending": timeline_pending,
                "commits_pending": commits_pending,
                "harvest_open": harvest_open,
                "history_completed": history_completed,
                "prs_missing_engagement": engagement_missing,
                "prs_engagement_incomplete": engagement_incomplete,
                "prs_missing_head_ci_state": missing_head_ci,
                "prs_missing_head_sha": missing_head_sha,
                "prs_missing_head_ci_contexts": missing_head_contexts,
            }
        )
    return {"repos": len(repos), "rows_created": rows, "per_repo": per_repo}
