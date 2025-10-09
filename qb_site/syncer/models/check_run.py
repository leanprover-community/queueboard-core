from __future__ import annotations

from django.db import models

from core.models.base import TimestampedModel
from .pull_request import PullRequest


class CheckRunStatus(models.TextChoices):
    QUEUED = "QUEUED", "queued"
    IN_PROGRESS = "IN_PROGRESS", "in_progress"
    COMPLETED = "COMPLETED", "completed"


class CheckRunConclusion(models.TextChoices):
    SUCCESS = "SUCCESS", "success"
    FAILURE = "FAILURE", "failure"
    CANCELLED = "CANCELLED", "cancelled"
    NEUTRAL = "NEUTRAL", "neutral"
    SKIPPED = "SKIPPED", "skipped"
    TIMED_OUT = "TIMED_OUT", "timed_out"
    ACTION_REQUIRED = "ACTION_REQUIRED", "action_required"


class CheckRun(TimestampedModel):
    """Historical GitHub Check Run for a PR's head commits.

    Notes
    - Rows are keyed by GitHub's global ``id`` (GraphQL node id); we expect this to be present,
      and enforce uniqueness via ``github_node_id``.
    - Timestamps from GitHub are prefixed with ``gh_`` to distinguish them from the row's own
      lifecycle timestamps.
    - Ordering for analytics uses ``gh_completed_at`` where available (fallbacks may be applied
      in Analyzer logic if needed).
    """

    pull_request = models.ForeignKey(PullRequest, on_delete=models.CASCADE, related_name="check_runs")

    # Global GraphQL id for the check run.
    github_node_id = models.CharField(max_length=255, unique=True)

    # Commit SHA this run applies to.
    head_sha = models.CharField(max_length=64)

    # Human-readable run name; matched (case-insensitively) in Analyzer for inessential classification.
    name = models.CharField(max_length=255)

    status = models.CharField(max_length=20, choices=CheckRunStatus.choices)
    conclusion = models.CharField(max_length=20, choices=CheckRunConclusion.choices, null=True, blank=True)

    details_url = models.URLField(null=True, blank=True)
    external_id = models.CharField(max_length=255, null=True, blank=True)

    gh_started_at = models.DateTimeField(null=True, blank=True)
    gh_completed_at = models.DateTimeField(null=True, blank=True)
    gh_updated_at = models.DateTimeField(null=True, blank=True)

    # Ingestion bookkeeping for incremental syncs.
    last_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            # Keep index name <= 30 chars for cross‑DB compatibility.
            models.Index(fields=["pull_request", "gh_completed_at"], name="ckrun_pr_comp_idx"),
        ]
        ordering = ["pull_request", "gh_completed_at", "id"]

    def __str__(self) -> str:  # pragma: no cover - simple representation
        return f"CheckRun({self.name})@{self.head_sha[:7]} for {self.pull_request}"
