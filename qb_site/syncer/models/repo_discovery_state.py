from __future__ import annotations

from django.db import models
from django.utils import timezone

from core.models import Repository
from core.models.base import TimestampedModel


class RepoDiscoveryState(TimestampedModel):
    """Per-repository updatedAt discovery watermark and continuation state."""

    repository = models.OneToOneField(Repository, on_delete=models.CASCADE, related_name="discovery_state")

    # Oldest cutoff fully scanned for updatedAt DESC discovery.
    last_successful_cutoff_at = models.DateTimeField(null=True, blank=True)
    # Fixed cutoff for an in-progress continuation run.
    continuation_cutoff_at = models.DateTimeField(null=True, blank=True)
    # GraphQL cursor for continuation pagination.
    continuation_cursor = models.TextField(null=True, blank=True)
    # Start time for the current continuation sequence.
    continuation_started_at = models.DateTimeField(null=True, blank=True)

    # Intended watermark target when a continuation was started by a fresh scan.
    # Set to fresh_base_cutoff on the fresh run that starts a continuation; preserved
    # across subsequent continuation batches; cleared on mark_success.  When a
    # continuation completes, the watermark advances to this value instead of the
    # (older) continuation_cutoff_at, so the system escapes a stale-watermark trap.
    continuation_success_cutoff = models.DateTimeField(null=True, blank=True)

    # Most recent attempted discovery run for this repository.
    last_attempted_at = models.DateTimeField(null=True, blank=True)
    # Most recent successful full cutoff scan completion.
    last_successful_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Repository discovery state"
        verbose_name_plural = "Repository discovery states"

    def mark_attempted(self) -> None:
        self.last_attempted_at = timezone.now()
        self.save(update_fields=["last_attempted_at", "updated_at"])

    def set_continuation(self, *, cutoff_at, cursor: str | None, success_cutoff=None) -> None:
        now = timezone.now()
        if self.continuation_started_at is None:
            self.continuation_started_at = now
        self.continuation_cutoff_at = cutoff_at
        self.continuation_cursor = cursor
        self.last_attempted_at = now
        if success_cutoff is not None:
            self.continuation_success_cutoff = success_cutoff
        self.save(
            update_fields=[
                "continuation_started_at",
                "continuation_cutoff_at",
                "continuation_cursor",
                "continuation_success_cutoff",
                "last_attempted_at",
                "updated_at",
            ]
        )

    def mark_success(self, *, cutoff_at) -> None:
        now = timezone.now()
        self.last_successful_cutoff_at = cutoff_at
        self.last_successful_at = now
        self.last_attempted_at = now
        self.continuation_cutoff_at = None
        self.continuation_cursor = None
        self.continuation_started_at = None
        self.continuation_success_cutoff = None
        self.save(
            update_fields=[
                "last_successful_cutoff_at",
                "last_successful_at",
                "last_attempted_at",
                "continuation_cutoff_at",
                "continuation_cursor",
                "continuation_started_at",
                "continuation_success_cutoff",
                "updated_at",
            ]
        )
