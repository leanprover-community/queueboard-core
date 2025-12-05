from __future__ import annotations

from django.db import models

from core.models.base import TimestampedModel
from core.models.repository import Repository


class PRDependency(TimestampedModel):
    """PR-level dependency inferred from a PR body.

    Represents an edge from ``pull_request`` to another PR number (usually in the
    same repository) that was referenced via a checkbox line in the PR body,
    e.g. ``- [ ] depends on: #123``.
    """

    pull_request = models.ForeignKey(
        "syncer.PullRequest",
        on_delete=models.CASCADE,
        related_name="dependencies",
    )
    depends_on_repository = models.ForeignKey(
        Repository,
        on_delete=models.CASCADE,
        related_name="dependency_edges",
    )
    depends_on_number = models.PositiveIntegerField()
    depends_on_pull_request = models.ForeignKey(
        "syncer.PullRequest",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="dependents",
    )

    class Meta:
        ordering = ["pull_request", "depends_on_repository", "depends_on_number", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["pull_request", "depends_on_repository", "depends_on_number"],
                name="analyzer_prdependency_unique_edge",
            ),
        ]
        indexes = [
            models.Index(
                fields=["depends_on_repository", "depends_on_number"],
                name="prdep_repo_num_idx",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - display helper
        return f"PRDependency(pr={self.pull_request_id}, depends_on={self.depends_on_repository_id}#{self.depends_on_number})"
