from __future__ import annotations

from django.db import models

from core.models import Repository


class AnalyzerConvergenceSnapshot(models.Model):
    """Periodic snapshot of analyzer convergence for a repository."""

    repository = models.ForeignKey(Repository, on_delete=models.CASCADE, related_name="analyzer_convergence_snapshots")
    collected_at = models.DateTimeField(db_index=True)

    pr_no_revisions = models.IntegerField(default=0)
    windows_stale = models.IntegerField(default=0)
    # Count of PRRevision head SHAs with no CI rows and no CIShaFetchState attempts.
    ci_not_checked = models.IntegerField(default=0)
    ci_gated_missing_windows = models.IntegerField(default=0)
    prs_missing_queue_window_rollups = models.IntegerField(default=0)
    prs_missing_dependency_state = models.IntegerField(default=0)
    prs_stale_dependency_state = models.IntegerField(default=0)
    windows_unknown_attribution = models.IntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-collected_at", "-id"]
        indexes = [
            models.Index(fields=["repository", "collected_at"], name="an_conv_repo_time_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - display
        return f"{self.repository} @ {self.collected_at.isoformat()}"
