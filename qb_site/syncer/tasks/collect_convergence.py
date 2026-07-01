from __future__ import annotations

from celery import shared_task
from django.conf import settings
from django.db.models import DateTimeField, ExpressionWrapper, F, Q, Exists, OuterRef
from django.utils import timezone

from syncer.models import (
    ArchiveImportItem,
    ArchiveImportItemStatus,
    PullRequest,
    CommitHistoryHarvest,
    RepoBackfillCursor,
    RepoDiscoveryState,
    SyncerConvergenceSnapshot,
    CommitCheckRun,
    CommitStatusContext,
)
from syncer.services.archive_import import archive_touched_resync_targets
from syncer.services.consistency import inconsistent_open_prs_queryset
from syncer.services.sync_schema_upgrades import CURRENT_SYNC_SCHEMA_VERSION
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
        discovery_catchup_lag_seconds = None
        if discovery_state and discovery_state.continuation_success_cutoff is not None and discovery_cutoff is not None:
            delta = (discovery_state.continuation_success_cutoff - discovery_cutoff).total_seconds()
            discovery_catchup_lag_seconds = max(0, int(delta))

        prs_below_target = qs.filter(sync_schema_version__lt=CURRENT_SYNC_SCHEMA_VERSION).count()

        archive_qs = ArchiveImportItem.objects.filter(repository=repo)
        archive_pending = archive_qs.filter(
            status__in=[
                ArchiveImportItemStatus.PENDING,
                ArchiveImportItemStatus.IN_PROGRESS,
                ArchiveImportItemStatus.FAILED_TRANSIENT,
            ]
        ).count()
        archive_completed = archive_qs.filter(status=ArchiveImportItemStatus.COMPLETED).count()
        archive_failed_permanent = archive_qs.filter(status=ArchiveImportItemStatus.FAILED_PERMANENT).count()
        archive_resync_remaining = archive_touched_resync_targets(repo).count()

        missing_head_ci = qs.filter(head_ci_state__isnull=True).count()
        missing_head_sha = qs.filter(Q(head_sha__isnull=True) | Q(head_sha="")).count()
        head_ccr = CommitCheckRun.objects.filter(repository=repo, head_sha=OuterRef("head_sha"))
        head_csc = CommitStatusContext.objects.filter(repository=repo, head_sha=OuterRef("head_sha"))
        missing_head_contexts = (
            qs.filter(state="open")
            .filter(head_sha__isnull=False)
            .exclude(head_sha="")
            .annotate(
                has_head_ccr=Exists(head_ccr),
                has_head_csc=Exists(head_csc),
            )
            .filter(has_head_ccr=False, has_head_csc=False)
            .count()
        )

        inconsistent_open = inconsistent_open_prs_queryset(repo).count()

        SyncerConvergenceSnapshot.objects.create(
            repository=repo,
            collected_at=collected_at,
            timeline_backfill_pending=timeline_pending,
            commits_backfill_pending=commits_pending,
            incomplete_prs=incomplete,
            harvest_jobs_open=harvest_open,
            history_cursor_completed=history_completed,
            discovery_lag_seconds=discovery_lag_seconds,
            discovery_catchup_lag_seconds=discovery_catchup_lag_seconds,
            discovery_continuation_active=discovery_continuation_active,
            discovery_last_attempted_at=discovery_state.last_attempted_at if discovery_state else None,
            discovery_last_successful_at=discovery_state.last_successful_at if discovery_state else None,
            prs_missing_head_ci_state=missing_head_ci,
            prs_missing_head_sha=missing_head_sha,
            prs_missing_head_ci_contexts=missing_head_contexts,
            inconsistent_open_prs=inconsistent_open,
            prs_below_current_sync_schema_version=prs_below_target,
            sync_schema_version_target=CURRENT_SYNC_SCHEMA_VERSION,
            archive_pending=archive_pending,
            archive_completed=archive_completed,
            archive_failed_permanent=archive_failed_permanent,
            archive_resync_remaining=archive_resync_remaining,
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
                "discovery_catchup_lag_seconds": discovery_catchup_lag_seconds,
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
                "prs_missing_head_ci_state": missing_head_ci,
                "prs_missing_head_sha": missing_head_sha,
                "prs_missing_head_ci_contexts": missing_head_contexts,
                "inconsistent_open_prs": inconsistent_open,
                "prs_below_current_sync_schema_version": prs_below_target,
                "sync_schema_version_target": CURRENT_SYNC_SCHEMA_VERSION,
                "archive_pending": archive_pending,
                "archive_completed": archive_completed,
                "archive_failed_permanent": archive_failed_permanent,
                "archive_resync_remaining": archive_resync_remaining,
            }
        )
    return {"repos": len(repos), "rows_created": rows, "per_repo": per_repo, "request_meta": request_meta}
