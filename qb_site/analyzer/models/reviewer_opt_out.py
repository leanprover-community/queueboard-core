from __future__ import annotations

from django.db import models

from core.models import Repository
from core.models.base import TimestampedModel


class ReviewerOptOut(TimestampedModel):
    """Records reviewer opt-outs from auto-assignment for a specific PR."""

    repository = models.ForeignKey(Repository, on_delete=models.CASCADE, related_name="reviewer_opt_outs")
    pr_number = models.PositiveIntegerField()
    reviewer_login = models.CharField(max_length=255)
    active = models.BooleanField(default=True)
    opted_out_at = models.DateTimeField()
    cleared_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["repository", "pr_number", "reviewer_login"],
                name="analyzer_revieweroptout_repo_pr_reviewer_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["repository", "pr_number", "active"], name="an_roo_pr_active_idx"),
            models.Index(fields=["repository", "reviewer_login", "active"], name="an_roo_reviewer_active_idx"),
        ]
        ordering = ["repository", "-opted_out_at", "id"]

    def __str__(self) -> str:  # pragma: no cover - simple representation
        return f"ReviewerOptOut(repo={self.repository}, pr={self.pr_number}, reviewer={self.reviewer_login})"
