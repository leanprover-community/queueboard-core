from __future__ import annotations

from django.db import models

from core.models import Repository
from core.models.base import TimestampedModel


class ReviewerAssignmentSnapshot(TimestampedModel):
    """Cached reviewer assignment payload derived from a queue snapshot."""

    repository = models.ForeignKey(Repository, on_delete=models.CASCADE, related_name="reviewer_assignment_snapshots")
    queue_snapshot = models.ForeignKey(
        "analyzer.QueueSnapshot",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewer_assignment_snapshots",
    )
    cache_key = models.CharField(max_length=128, default="default")

    generated_at = models.DateTimeField()
    payload = models.JSONField()
    etag = models.CharField(max_length=128)
    assignment_count = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["repository", "cache_key"],
                name="analyzer_reviewerassignmentsnapshot_repo_cache_key_unique",
            ),
        ]
        ordering = ["repository", "-generated_at", "id"]

    def __str__(self) -> str:  # pragma: no cover - simple representation
        return f"ReviewerAssignmentSnapshot(repo={self.repository}, key={self.cache_key})"
