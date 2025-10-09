from __future__ import annotations

from django.db import models

from core.models.base import TimestampedModel
from .pull_request import PullRequest


class StatusContextState(models.TextChoices):
    SUCCESS = "SUCCESS", "success"
    FAILURE = "FAILURE", "failure"
    ERROR = "ERROR", "error"
    PENDING = "PENDING", "pending"


class StatusContext(TimestampedModel):
    """Historical GitHub Status Context for a PR's head commits (REST Statuses API).

    Notes
    - Commit statuses are append-only. We store each status row with its REST ``id`` and use
      ``gh_created_at`` as the canonical timestamp for ordering.
    - We avoid confusion with the model's own lifecycle timestamps by prefixing provider times
      with ``gh_``.
    """

    pull_request = models.ForeignKey(PullRequest, on_delete=models.CASCADE, related_name="status_contexts")

    # REST statuses API returns a numeric id; store as BigInteger.
    rest_id = models.BigIntegerField(unique=True)

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
        ordering = ["pull_request", "gh_created_at", "id"]

    def __str__(self) -> str:  # pragma: no cover - simple representation
        return f"StatusContext({self.name}={self.state})@{self.head_sha[:7]} for {self.pull_request}"
