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
    prs_missing_engagement = models.IntegerField(default=0)
    prs_engagement_incomplete = models.IntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-collected_at", "-id"]
        indexes = [
            models.Index(fields=["repository", "collected_at"], name="sync_conv_repo_time_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - display
        return f"{self.repository} @ {self.collected_at.isoformat()}"
