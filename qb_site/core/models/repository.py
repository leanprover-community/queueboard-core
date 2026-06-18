from __future__ import annotations

from django.db import models

from core.services.topic_labels import validate_topic_label_pattern

from .base import TimestampedModel


class Repository(TimestampedModel):
    """Canonical identity for a GitHub repository.

    Notes
    - Example for owner/name: leanprover-community/mathlib4
    - The ``github_node_id`` stored here is the global GraphQL/REST node id (REST exposes this
      as the string field ``node_id``; GraphQL uses it as ``id``). It is distinct from the REST
      numeric ``id`` and is suitable for idempotent upserts.
    - ``default_branch`` mirrors the repository's default branch reported by GitHub (e.g., "master"
      or "main"). Dashboards/analytics may use this to distinguish "queue" vs. "other base".
    """

    # GitHub owner/organization and repository name. Unique together.
    owner = models.CharField(max_length=255)
    name = models.CharField(max_length=255)

    # Global node ID from GitHub (GraphQL ``id`` / REST ``node_id``). Optional during bootstrap.
    # Keeping this allows stable cross‑API upserts regardless of whether we call REST or GraphQL.
    github_node_id = models.CharField(max_length=255, unique=True, null=True, blank=True)

    # Default branch as reported by GitHub (e.g., "master" or "main").
    default_branch = models.CharField(max_length=100)

    # Operational toggle for future use (e.g., filter for active repos in schedulers/UIs).
    is_active = models.BooleanField(default=True)

    # Optional per-repo CI tracking filters. When non-empty, these act as allowlists
    # for Syncer ingestion, matched as case-insensitive substrings against
    # Commit-scoped check run and status context names respectively. If empty, global
    # SYNCER_CI_* settings apply.
    ci_tracked_checkrun_names = models.JSONField(default=list, blank=True)
    ci_tracked_status_names = models.JSONField(default=list, blank=True)

    # Per-repo definition of reviewer "topic" labels: a case-insensitive regex matched
    # (full-match) against label names. Topic labels are the labels offered in the reviewer
    # preferences form and matched against each reviewer's preferred labels during
    # auto-assignment. Leave blank to use the default (``t-.*|ci|imo|tech debt|documentation``).
    # See core.services.topic_labels.
    assignment_topic_label_pattern = models.CharField(
        max_length=500,
        blank=True,
        default="",
        validators=[validate_topic_label_pattern],
        help_text=(
            "Case-insensitive regex (full-match) selecting which label names count as reviewer "
            "topic labels for auto-assignment and the preferences form. Blank uses the default: "
            "t-.*|ci|imo|tech debt|documentation"
        ),
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["owner", "name"], name="core_repository_owner_name_unique"),
        ]

    def __str__(self) -> str:  # pragma: no cover - simple representation
        return f"{self.owner}/{self.name}"
