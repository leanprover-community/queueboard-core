from __future__ import annotations

from django.db import models
from django.utils import timezone

from core.models.base import TimestampedModel
from core.models import Repository


class RepoBackfillCursor(TimestampedModel):
    """Per-repository cursor for history backfill.

    Tracks how far we have walked a repository's PR history (by createdAt)
    when enqueuing PR sync tasks for historical backfill.
    """

    repository = models.OneToOneField(Repository, on_delete=models.CASCADE, related_name="backfill_cursor")

    # GraphQL cursor for the next page of pullRequests(createdAt ASC) to backfill.
    created_cursor = models.TextField(null=True, blank=True)

    # Oldest PR createdAt we've seen during backfill (informational only).
    oldest_created_at = models.DateTimeField(null=True, blank=True)

    # Flag indicating that we've reached the end of history for this repo.
    completed = models.BooleanField(default=False)

    # Last time a backfill run touched this cursor.
    last_run_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Repository backfill cursor"
        verbose_name_plural = "Repository backfill cursors"

    def mark_completed(self) -> None:
        self.completed = True
        self.last_run_at = timezone.now()
        self.save(update_fields=["completed", "last_run_at"])
