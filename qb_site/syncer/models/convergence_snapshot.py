from __future__ import annotations

from django.db import models

from core.models import Repository


class SyncerConvergenceSnapshot(models.Model):
    """Periodic snapshot of backfill/convergence state for a repository."""

    repository = models.ForeignKey(Repository, on_delete=models.CASCADE, related_name="syncer_convergence_snapshots")
    collected_at = models.DateTimeField(db_index=True)

    # Backfill status
    timeline_backfill_pending = models.IntegerField(default=0)
    commits_backfill_pending = models.IntegerField(default=0)
    incomplete_prs = models.IntegerField(default=0)
    harvest_jobs_open = models.IntegerField(default=0)
    history_cursor_completed = models.BooleanField(default=False)
    discovery_lag_seconds = models.IntegerField(null=True, blank=True)
    # When a catch-up continuation is in progress, the number of seconds between
    # last_successful_cutoff_at and continuation_success_cutoff — i.e. how much
    # watermark advancement the running continuation still needs to deliver.
    # Null when no catch-up continuation is active.  Should trend toward zero
    # as the continuation makes progress and reaches zero on completion.
    discovery_catchup_lag_seconds = models.IntegerField(null=True, blank=True)
    discovery_continuation_active = models.BooleanField(default=False)
    discovery_last_attempted_at = models.DateTimeField(null=True, blank=True)
    discovery_last_successful_at = models.DateTimeField(null=True, blank=True)
    prs_missing_engagement = models.IntegerField(default=0)
    prs_engagement_incomplete = models.IntegerField(default=0)
    prs_missing_head_ci_state = models.IntegerField(default=0)
    prs_missing_head_sha = models.IntegerField(default=0)
    prs_missing_head_ci_contexts = models.IntegerField(default=0)
    # Wave progress for the sync_schema_version upgrader.
    # ``sync_schema_version_target`` records the value of CURRENT_SYNC_SCHEMA_VERSION
    # in the codebase at collection time, so historical rows remain interpretable
    # after future bumps. ``prs_below_current_sync_schema_version`` is the
    # operator's primary canary for a stalled wave.
    prs_below_current_sync_schema_version = models.IntegerField(default=0)
    sync_schema_version_target = models.PositiveSmallIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-collected_at", "-id"]
        indexes = [
            models.Index(fields=["repository", "collected_at"], name="sync_conv_repo_time_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - display
        return f"{self.repository} @ {self.collected_at.isoformat()}"
