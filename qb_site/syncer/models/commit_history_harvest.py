from __future__ import annotations

from django.db import models

from syncer.models.pull_request import PullRequest


class CommitHistoryHarvest(models.Model):
    """Cursor/state for harvesting commit history from a starting SHA."""

    pull_request = models.ForeignKey(
        PullRequest,
        on_delete=models.CASCADE,
        related_name="commit_history_harvests",
    )
    start_sha = models.CharField(max_length=40)
    cursor = models.TextField(null=True, blank=True)
    has_more = models.BooleanField(default=True)
    cutoff_ts = models.DateTimeField(null=True, blank=True)
    last_harvested_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("pull_request", "start_sha")
        indexes = [
            models.Index(fields=["pull_request", "start_sha"], name="chh_pr_sha_idx"),
            models.Index(fields=["has_more"], name="chh_has_more_idx"),
        ]
        ordering = ["-updated_at", "-id"]

    def __str__(self) -> str:  # pragma: no cover - simple representation
        return f"CommitHistoryHarvest(pr={self.pull_request_id}, start_sha={self.start_sha[:7]}, has_more={self.has_more})"
