from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models

from .base import TimestampedModel
from .repository import Repository
from .user import User


def clean_label_names(value: object) -> list[str]:
    """A stored label-list JSON value as label names: stripped, non-empty strings, in order.

    The columns are free JSON, so an admin edit or an import can leave a bare string, ``null``
    entries or numbers in them. A bare string is one label (iterating it would yield characters),
    any other non-list is empty, and non-string entries are dropped. Every reader of
    ``excluded_labels`` goes through this so the engine, the form and the importer agree on what
    the reviewer excluded.
    """
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def overlapping_labels(preferred: object, excluded: object) -> list[str]:
    """The ``excluded`` labels that are also in ``preferred``, compared case-insensitively.

    A label cannot be both wanted and refused. The engine would let the exclusion win, but a silent
    tie-break is worse than asking which one was meant, so every write path refuses the overlap:
    ``ReviewerPreference.clean`` (the console form and Django admin) and the reviewer-topics
    importer (design doc 057).
    """
    preferred_keys = {name.casefold() for name in clean_label_names(preferred)}
    return [name for name in clean_label_names(excluded) if name.casefold() in preferred_keys]


class ReviewerPreference(TimestampedModel):
    """Repo‑scoped reviewer preferences.

    Fields
    - ``repository``/``user``: scope and identity; unique together.
    - ``maximum_capacity``: numeric cap for concurrently assigned PRs (legacy default is 10).
    - ``max_new_assignments_per_week``: optional rolling-window cap on *new* assignments (design doc
      054). ``None`` (the default) means unlimited. Orthogonal to ``maximum_capacity``: that one
      bounds the stock a reviewer holds at once, this one bounds the flow they take on, so a
      reviewer who clears PRs quickly is no longer refilled without limit.
    - ``auto_assign``: whether the reviewer participates in auto‑assignment.
    - ``away_until``: optional break end timestamp (timezone‑aware). Suggestions should skip the
      reviewer while ``now_utc < away_until``.
    - ``preferred_labels``: list of GitHub label names the reviewer prefers (e.g., ["t-analysis", "t-algebra"]).
    - ``free_form``: optional free‑text notes from reviewer‑topics.json for context.
    - ``conflict_of_interest``: list of GitHub handles this reviewer should not be auto-assigned to.
    - ``excluded_labels``: list of GitHub label names; a PR carrying any of them is never
      auto-assigned (or proposed, or suggested) to this reviewer (design doc 057). Any label counts,
      not only topic labels. Private, like ``conflict_of_interest``: never served by a public API.
    - ``notifications_enabled``: whether reviewer receives queue nudge notifications.
    - ``notification_settings``: extensible JSON settings for notification policy (for example X/Y thresholds).
    - ``assignment_acceptance``: ``auto`` (direct-assign like today) or ``confirm`` (propose and require
      the reviewer to accept before the assignment is executed). New rows default to ``confirm``;
      existing rows are backfilled to ``auto`` (see design doc 050).
    """

    ACCEPTANCE_AUTO = "auto"
    ACCEPTANCE_CONFIRM = "confirm"
    ACCEPTANCE_CHOICES = [
        (ACCEPTANCE_AUTO, "auto"),
        (ACCEPTANCE_CONFIRM, "confirm"),
    ]

    repository = models.ForeignKey(Repository, on_delete=models.CASCADE, related_name="reviewer_preferences")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="reviewer_preferences")

    maximum_capacity = models.PositiveIntegerField(default=10)
    # Rolling-window cap on newly assigned PRs (design doc 054). ``None`` = unlimited, which is the
    # opt-in default: the weekly gate is skipped entirely and behavior is unchanged. The window
    # length is operational (``ANALYZER_ASSIGNMENT_RATE_WINDOW_DAYS``, 7 days), not per-reviewer.
    max_new_assignments_per_week = models.PositiveIntegerField(null=True, blank=True)
    auto_assign = models.BooleanField(default=True)
    # Whether automatic assignment goes through the acceptance gate ("confirm") or assigns
    # directly like the legacy behavior ("auto"). New reviewers default to "confirm"; existing
    # reviewers are backfilled to "auto" by the accompanying data migration (design doc 050).
    assignment_acceptance = models.CharField(
        max_length=16,
        choices=ACCEPTANCE_CHOICES,
        default=ACCEPTANCE_CONFIRM,
    )
    away_until = models.DateTimeField(null=True, blank=True)

    # Store as a JSON array of strings (label names). Examples: ["t-analysis", "tech debt"].
    preferred_labels = models.JSONField(default=list, blank=True)

    # Free-form notes/comments provided by the reviewer (legacy: reviewer-topics.json "free_form").
    free_form = models.TextField(null=True, blank=True)
    # GitHub handles that should not be auto-assigned to this reviewer (legacy: conflict_of_interest list).
    conflict_of_interest = models.JSONField(default=list, blank=True)
    # Label names whose PRs should never be auto-assigned to this reviewer (design doc 057). Stored like
    # ``preferred_labels`` (JSON names, no FK; design doc 003) and matched case-insensitively against
    # *every* label on the PR. Private: keep it off public surfaces such as the reviewer-interests API.
    excluded_labels = models.JSONField(default=list, blank=True)
    notifications_enabled = models.BooleanField(default=False)
    # Extensible settings for queue nudge policy.
    # Initial keys are expected to include:
    # - stale_nudge_days (X)
    # - auto_unassign_days (Y)
    notification_settings = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["repository", "user"], name="core_reviewer_preference_repo_user_unique"),
        ]

    def __str__(self) -> str:  # pragma: no cover - simple representation
        return f"{self.user} @ {self.repository}"

    def clean(self) -> None:
        super().clean()
        overlap = overlapping_labels(self.preferred_labels, self.excluded_labels)
        if overlap:
            raise ValidationError({"excluded_labels": f"Also selected as a preferred label: {', '.join(overlap)}."})
