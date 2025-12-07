from __future__ import annotations

from django.db import models

from core.models import Repository
from core.models.base import TimestampedModel


class QueueSnapshot(TimestampedModel):
    """Cached queueboard snapshot payload for a repository.

    A single row stores the rendered snapshot JSON plus basic metadata.
    `cache_key` is a flexible string that can encode the ruleset or other
    variant identifiers without hard-coupling to a specific FK.
    """

    repository = models.ForeignKey(Repository, on_delete=models.CASCADE, related_name="queue_snapshots")
    cache_key = models.CharField(max_length=128, default="default")

    generated_at = models.DateTimeField()
    payload = models.JSONField()
    etag = models.CharField(max_length=128)
    pr_count = models.PositiveIntegerField()
    queue_count = models.PositiveIntegerField()
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["repository", "cache_key"],
                name="analyzer_queuesnapshot_repo_cache_key_unique",
            ),
        ]
        ordering = ["repository", "-generated_at", "id"]

    def __str__(self) -> str:  # pragma: no cover - simple representation
        return f"QueueSnapshot(repo={self.repository}, key={self.cache_key})"
