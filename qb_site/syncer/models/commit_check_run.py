from __future__ import annotations

from django.db import models
from django.db.models import Q

from core.models import Repository
from core.models.base import TimestampedModel
from .ci_enums import CheckRunConclusion, CheckRunStatus


class CommitCheckRun(TimestampedModel):
    """Commit-scoped check run facts keyed by repository+head SHA."""

    repository = models.ForeignKey(Repository, on_delete=models.CASCADE, related_name="commit_check_runs")
    github_node_id = models.CharField(max_length=255, null=True, blank=True)
    head_sha = models.CharField(max_length=64)
    name = models.CharField(max_length=255)
    status = models.CharField(max_length=20, choices=CheckRunStatus.choices)
    conclusion = models.CharField(max_length=20, choices=CheckRunConclusion.choices, null=True, blank=True)
    details_url = models.URLField(null=True, blank=True)
    external_id = models.CharField(max_length=255, null=True, blank=True)
    gh_started_at = models.DateTimeField(null=True, blank=True)
    gh_completed_at = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["repository", "head_sha"], name="cckr_repo_sha_idx"),
            models.Index(fields=["repository", "gh_completed_at"], name="cckr_repo_comp_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["github_node_id"],
                name="cckr_nodeid_uniq",
                condition=Q(github_node_id__isnull=False),
            ),
            models.UniqueConstraint(
                fields=["repository", "head_sha", "name", "external_id"],
                name="cckr_repo_sha_name_ext_uniq",
                condition=Q(external_id__isnull=False),
            ),
        ]
        ordering = ["repository", "head_sha", "gh_completed_at", "id"]

    def __str__(self) -> str:  # pragma: no cover - simple representation
        return f"CommitCheckRun({self.name})@{self.head_sha[:7]} in {self.repository}"
