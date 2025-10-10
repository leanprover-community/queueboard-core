from __future__ import annotations

from django.db import models
from django.db.models import Q

from core.models.base import TimestampedModel
from .pull_request import PullRequest


class StatusContextState(models.TextChoices):
    SUCCESS = "SUCCESS", "success"
    FAILURE = "FAILURE", "failure"
    ERROR = "ERROR", "error"
    PENDING = "PENDING", "pending"


class StatusContext(TimestampedModel):
    """Per-commit status contexts for a PR's head commits.

    Notes
    - Snapshots: GraphQL statusCheckRollup provides the latest state per context for a commit. We
      store these with ``github_node_id`` when present.
    - History (optional): REST Statuses API is append-only; we store historical rows with ``rest_id``
      and order by ``gh_created_at``.
    - Provider timestamps are prefixed with ``gh_`` to distinguish them from the row lifecycle
      timestamps from the abstract base model.
    """

    pull_request = models.ForeignKey(PullRequest, on_delete=models.CASCADE, related_name="status_contexts")

    # GraphQL snapshot id for the context (nullable if sourced only from REST history).
    github_node_id = models.CharField(max_length=255, null=True, blank=True)

    # REST statuses API returns a numeric id; store as BigInteger (nullable if sourced only from GraphQL).
    rest_id = models.BigIntegerField(null=True, blank=True)

    # Commit SHA this status applies to.
    head_sha = models.CharField(max_length=64)

    # The status "context" name; Analyzer matches this (case-insensitively) for inessential classification.
    name = models.CharField(max_length=255)

    state = models.CharField(max_length=20, choices=StatusContextState.choices)

    target_url = models.URLField(null=True, blank=True)
    description = models.TextField(null=True, blank=True)

    gh_created_at = models.DateTimeField()

    # Ingestion bookkeeping for incremental syncs.
    last_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            # Keep index name <= 30 chars for cross‑DB compatibility.
            models.Index(fields=["pull_request", "gh_created_at"], name="stctx_pr_created_idx"),
        ]
        constraints = [
            # Enforce uniqueness for provided provider ids when present.
            models.UniqueConstraint(
                fields=["github_node_id"],
                name="stctx_nodeid_uniq",
                condition=Q(github_node_id__isnull=False),
            ),
            models.UniqueConstraint(
                fields=["rest_id"],
                name="stctx_restid_uniq",
                condition=Q(rest_id__isnull=False),
            ),
        ]
        ordering = ["pull_request", "gh_created_at", "id"]

    def __str__(self) -> str:  # pragma: no cover - simple representation
        return f"StatusContext({self.name}={self.state})@{self.head_sha[:7]} for {self.pull_request}"
