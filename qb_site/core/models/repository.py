from __future__ import annotations

from django.db import models

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

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["owner", "name"], name="core_repository_owner_name_unique"),
        ]

    def __str__(self) -> str:  # pragma: no cover - simple representation
        return f"{self.owner}/{self.name}"
