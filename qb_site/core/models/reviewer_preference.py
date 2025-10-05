from __future__ import annotations

from django.db import models

from .base import TimestampedModel
from .repository import Repository
from .user import User


class ReviewerPreference(TimestampedModel):
    """Repo‑scoped reviewer preferences.

    Fields
    - ``repository``/``user``: scope and identity; unique together.
    - ``maximum_capacity``: numeric cap for concurrently assigned PRs (legacy default is 10).
    - ``auto_assign``: whether the reviewer participates in auto‑assignment.
    - ``away_until``: optional break end timestamp (timezone‑aware). Suggestions should skip the
      reviewer while ``now_utc < away_until``.
    - ``preferred_labels``: list of GitHub label names the reviewer prefers (e.g., ["t-analysis", "t-algebra"]).
    - ``free_form``: optional free‑text notes from reviewer‑topics.json for context.
    """

    repository = models.ForeignKey(Repository, on_delete=models.CASCADE, related_name="reviewer_preferences")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="reviewer_preferences")

    maximum_capacity = models.PositiveIntegerField(default=10)
    auto_assign = models.BooleanField(default=True)
    away_until = models.DateTimeField(null=True, blank=True)

    # Store as a JSON array of strings (label names). Examples: ["t-analysis", "tech debt"].
    preferred_labels = models.JSONField(default=list, blank=True)

    # Free-form notes/comments provided by the reviewer (legacy: reviewer-topics.json "free_form").
    free_form = models.TextField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["repository", "user"], name="core_reviewer_preference_repo_user_unique"),
        ]

    def __str__(self) -> str:  # pragma: no cover - simple representation
        return f"{self.user} @ {self.repository}"
