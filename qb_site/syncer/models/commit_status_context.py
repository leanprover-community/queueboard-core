from __future__ import annotations

from django.db import models
from django.db.models import Q

from core.models import Repository
from core.models.base import TimestampedModel
from .ci_enums import StatusContextState


class CommitStatusContext(TimestampedModel):
    """Commit-scoped status context facts keyed by repository+head SHA."""

    repository = models.ForeignKey(Repository, on_delete=models.CASCADE, related_name="commit_status_contexts")
    github_node_id = models.CharField(max_length=255, null=True, blank=True)
    rest_id = models.BigIntegerField(null=True, blank=True)
    head_sha = models.CharField(max_length=64)
    name = models.CharField(max_length=255)
    state = models.CharField(max_length=20, choices=StatusContextState.choices)
    target_url = models.URLField(null=True, blank=True)
    description = models.TextField(null=True, blank=True)
    gh_created_at = models.DateTimeField()
    last_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["repository", "head_sha"], name="cscx_repo_sha_idx"),
            models.Index(fields=["repository", "gh_created_at"], name="cscx_repo_crtd_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["github_node_id"],
                name="cscx_nodeid_uniq",
                condition=Q(github_node_id__isnull=False),
            ),
            models.UniqueConstraint(
                fields=["rest_id"],
                name="cscx_restid_uniq",
                condition=Q(rest_id__isnull=False),
            ),
        ]
        ordering = ["repository", "head_sha", "gh_created_at", "id"]

    def __str__(self) -> str:  # pragma: no cover - simple representation
        return f"CommitStatusContext({self.name}={self.state})@{self.head_sha[:7]} in {self.repository}"
