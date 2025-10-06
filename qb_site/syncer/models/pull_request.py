from __future__ import annotations

from django.db import models

from core.models.base import TimestampedModel
from core.models.repository import Repository
from core.models.user import User


class PullRequestState(models.TextChoices):
    OPEN = "open", "open"
    CLOSED = "closed", "closed"


class PullRequest(TimestampedModel):
    """Raw Pull Request entity synced from GitHub

    Scope
    - Belongs to a single repository (``repository``) and is identified there by ``number``.

    Design (minimal, focused on queueboard parity)
    - We intentionally omit GitHub specific IDs (``node_id``/numeric ``id``). Upserts can
      key on ``(repository, number)`` and these IDs can be added later without breaking callers.
    - CI, labels, assignees, reviews, and timeline events are modeled in separate tables and joined
      when computing dashboard views.

    Indexes (why these)
    - Unique ``(repository, number)`` for identity and idempotent upserts.
    - Composite ``(repository, state)`` to quickly filter open PRs per repo (the usual entry point).
    - Composite ``(repository, gh_updated_at)`` to support recency sorting within a repo; Postgres
      can scan this btree in reverse for ``ORDER BY gh_updated_at DESC``.
    """

    # Identity
    repository = models.ForeignKey(Repository, on_delete=models.CASCADE, related_name="pull_requests")
    number = models.PositiveIntegerField()
    author = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="pull_requests")

    # State and timing
    state = models.CharField(max_length=10, choices=PullRequestState.choices)
    is_draft = models.BooleanField(default=False)
    gh_created_at = models.DateTimeField()
    gh_updated_at = models.DateTimeField()
    closed_at = models.DateTimeField(null=True, blank=True)
    merged_at = models.DateTimeField(null=True, blank=True)

    # Branches and fork context
    base_ref_name = models.CharField(max_length=255)
    head_ref_name = models.CharField(max_length=255)
    head_repo_owner_login = models.CharField(max_length=255)
    head_repo_name = models.CharField(max_length=255)

    # Content and sizes
    title = models.CharField(max_length=512)
    body = models.TextField()
    additions = models.IntegerField()
    deletions = models.IntegerField()
    changed_files_count = models.IntegerField()

    # Ingestion metadata
    last_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["repository", "number"], name="syncer_pullrequest_repo_number_unique"),
        ]
        indexes = [
            models.Index(fields=["repository", "state"], name="syncer_pr_repo_state_idx"),
            models.Index(fields=["repository", "gh_updated_at"], name="syncer_pr_repo_updated_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - simple representation
        return f"PR #{self.number} @ {self.repository}"
