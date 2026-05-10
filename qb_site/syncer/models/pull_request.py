from __future__ import annotations

from django.db import models

from core.models.base import TimestampedModel
from core.models.repository import Repository
from core.models.user import User


class PullRequestState(models.TextChoices):
    OPEN = "open", "open"
    CLOSED = "closed", "closed"
    MERGED = "merged", "merged"


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
    head_sha = models.CharField(max_length=64, null=True, blank=True)
    head_repo_owner_login = models.CharField(max_length=255)
    head_repo_name = models.CharField(max_length=255)

    # Content and sizes
    title = models.CharField(max_length=512)
    body = models.TextField()
    additions = models.IntegerField()
    deletions = models.IntegerField()
    changed_files_count = models.IntegerField()
    files = models.JSONField(default=list)
    assignees = models.JSONField(default=list)
    approvals = models.JSONField(default=list)
    commenters = models.JSONField(default=list)
    number_total_comments = models.IntegerField(null=True, blank=True)

    # Ingestion metadata
    last_synced_at = models.DateTimeField(null=True, blank=True)
    files_incomplete = models.BooleanField(default=False)
    assignees_incomplete = models.BooleanField(default=False)
    reviews_incomplete = models.BooleanField(default=False)
    comments_incomplete = models.BooleanField(default=False)

    # Latest processed assignment/unassignment event timestamp (monotonic).
    last_assignment_event_at = models.DateTimeField(null=True, blank=True)

    # Timeline backfill state (optional V1.1 feature)
    # - timeline_backfill_cursor: oldest cursor reached so far when paging backward
    #   using last/before. Seeded from the bundle's pageInfo.startCursor.
    # - timeline_backfill_done: set True when hasPreviousPage==False (no older pages).
    # - timeline_earliest_synced_at: convenience timestamp of earliest event persisted.
    timeline_backfill_cursor = models.TextField(null=True, blank=True)
    timeline_backfill_done = models.BooleanField(default=False)
    timeline_earliest_synced_at = models.DateTimeField(null=True, blank=True)

    # Commits backfill state (optional V1.1.1 feature)
    # Mirrors the timeline backfill tracking to enable admin visibility and paging continuity
    # when walking the commits connection backward.
    commits_backfill_cursor = models.TextField(null=True, blank=True)
    commits_backfill_done = models.BooleanField(default=False)
    commits_earliest_synced_at = models.DateTimeField(null=True, blank=True)

    # Head commit rollup status (GitHub statusCheckRollup.state) for coarse CI signal.
    head_ci_state = models.CharField(max_length=20, null=True, blank=True)

    # Set by the archive backfill importer (design doc 043) when this row was
    # created from a legacy queueboard-archive snapshot. Internal-only
    # provenance; not exposed in /api/v1/queueboard/snapshot. Never touched by
    # the live syncer's own writes.
    archive_imported_at = models.DateTimeField(null=True, blank=True)

    # Sync schema version. Owned exclusively by the upgrader registry in
    # qb_site/syncer/services/sync_schema_upgrades.py — never written by
    # PRSyncService. The periodic upgrader task selects PRs where this column
    # is below CURRENT_SYNC_SCHEMA_VERSION and dispatches per-version upgraders.
    # Indexed so the dispatcher's `< CURRENT` scan stays O(rows-needing-work)
    # at steady state.
    sync_schema_version = models.PositiveSmallIntegerField(default=0, db_index=True)

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
