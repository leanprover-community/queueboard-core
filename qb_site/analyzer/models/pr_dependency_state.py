from __future__ import annotations

from django.db import models


class PRDependencyState(models.Model):
    """Tracking metadata for dependency parsing per PR."""

    pull_request = models.OneToOneField(
        "syncer.PullRequest",
        on_delete=models.CASCADE,
        related_name="dependency_state",
    )
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_body_hash = models.CharField(max_length=64, blank=True)
    builder_version = models.PositiveIntegerField(default=1)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "-id"]
        indexes = [
            models.Index(fields=["last_checked_at"], name="prdepstate_last_checked_idx"),
            models.Index(fields=["builder_version"], name="prdepstate_builder_version_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - display helper
        return f"PRDependencyState(pr={self.pull_request_id}, checked_at={self.last_checked_at})"
