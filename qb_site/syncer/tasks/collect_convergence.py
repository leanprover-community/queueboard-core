from __future__ import annotations

from celery import shared_task
from django.conf import settings
from django.db.models import DateTimeField, ExpressionWrapper, F, Q, Exists, OuterRef
from django.utils import timezone

from syncer.models import (
    PullRequest,
    CommitHistoryHarvest,
    RepoBackfillCursor,
    RepoDiscoveryState,
    SyncerConvergenceSnapshot,
    CheckRun,
    CommitCheckRun,
    CommitStatusContext,
    StatusContext,
)
from core.models import Repository


@shared_task(name="syncer.collect_convergence", bind=True)
def collect_syncer_convergence_task(self) -> dict:  # type: ignore[no-redef]
    """Collect backfill/convergence counts per active repository."""
    headers = getattr(self.request, "headers", {}) or {}
    enqueue_source = headers.get("qb_enqueue_source") if isinstance(headers, dict) else None
    delivery_info = getattr(self.request, "delivery_info", {}) or {}
    request_meta = {
        "id": getattr(self.request, "id", None),
        "root_id": getattr(self.request, "root_id", None),
        "parent_id": getattr(self.request, "parent_id", None),
        "queue": delivery_info.get("routing_key") if isinstance(delivery_info, dict) else None,
        "exchange": delivery_info.get("exchange") if isinstance(delivery_info, dict) else None,
        "enqueue_source": enqueue_source or "unknown",
        "enqueued_at": headers.get("qb_enqueued_at") if isinstance(headers, dict) else None,
    }
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
        discovery_state = RepoDiscoveryState.objects.filter(repository=repo).first()
        discovery_cutoff = discovery_state.last_successful_cutoff_at if discovery_state else None
        discovery_lag_seconds = None
        if discovery_cutoff is not None:
            discovery_lag_seconds = max(0, int((collected_at - discovery_cutoff).total_seconds()))
        discovery_continuation_active = bool(
            discovery_state and discovery_state.continuation_cutoff_at is not None and discovery_state.continuation_cursor
        )

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
        head_ccr = CommitCheckRun.objects.filter(repository=repo, head_sha=OuterRef("head_sha"))
        head_csc = CommitStatusContext.objects.filter(repository=repo, head_sha=OuterRef("head_sha"))
        missing_head_contexts = (
            qs.filter(state="open")
            .filter(head_sha__isnull=False)
            .exclude(head_sha="")
            .annotate(
                has_head_cr=Exists(head_cr),
                has_head_sc=Exists(head_sc),
                has_head_ccr=Exists(head_ccr),
                has_head_csc=Exists(head_csc),
            )
            .filter(has_head_cr=False, has_head_sc=False, has_head_ccr=False, has_head_csc=False)
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
            discovery_lag_seconds=discovery_lag_seconds,
            discovery_continuation_active=discovery_continuation_active,
            discovery_last_attempted_at=discovery_state.last_attempted_at if discovery_state else None,
            discovery_last_successful_at=discovery_state.last_successful_at if discovery_state else None,
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
                "discovery_lag_seconds": discovery_lag_seconds,
                "discovery_continuation_active": discovery_continuation_active,
                "discovery_last_attempted_at": (
                    discovery_state.last_attempted_at.isoformat()
                    if discovery_state and discovery_state.last_attempted_at
                    else None
                ),
                "discovery_last_successful_at": (
                    discovery_state.last_successful_at.isoformat()
                    if discovery_state and discovery_state.last_successful_at
                    else None
                ),
                "prs_missing_engagement": engagement_missing,
                "prs_engagement_incomplete": engagement_incomplete,
                "prs_missing_head_ci_state": missing_head_ci,
                "prs_missing_head_sha": missing_head_sha,
                "prs_missing_head_ci_contexts": missing_head_contexts,
            }
        )
    return {"repos": len(repos), "rows_created": rows, "per_repo": per_repo, "request_meta": request_meta}
