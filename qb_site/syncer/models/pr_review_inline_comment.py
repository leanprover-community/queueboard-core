from __future__ import annotations

from django.db import models

from core.models.base import TimestampedModel
from .pr_timeline_event import PRTimelineEvent
from .pull_request import PullRequest


class PRReviewInlineComment(TimestampedModel):
    """Per-inline-comment row mirroring GraphQL ``PullRequestReviewComment``.

    Lives in its own table (rather than ``PRTimelineEvent``) because GitHub's
    inline review comments are nested under ``PullRequestReview.comments`` and
    are *not* members of the PR's ``timelineItems`` connection. Keeping
    ``PRTimelineEvent`` 1:1 with timeline-item nodes preserves a clean
    invariant for downstream consumers; threading information lives here.

    Idempotency
    - ``github_node_id`` is globally unique on GitHub, so a plain
      ``unique=True`` is enough to support ``bulk_create(ignore_conflicts=True)``
      on re-ingest.

    Linkage
    - ``review_node_id`` is the durable link to the parent
      ``PullRequestReview``: even if the parent ``PRTimelineEvent`` row is ever
      reconstituted, comments can still be re-associated by node id.
    - ``parent_review_event`` is the convenience FK for ORM joins; nullable
      with ``on_delete=SET_NULL`` so deleting the parent doesn't cascade.

    Threading (best-effort, reconciled on rewalk)
    - ``thread_root_node_id`` is the root of the ``replyTo`` chain at ingest
      time. Computed by walking ``replyTo`` within the in-flight set; if a
      comment's ``replyTo`` points outside the bundle, ``reply_to_node_id`` is
      used as the root and the value can be tightened on a later rewalk.
    """

    pull_request = models.ForeignKey(
        PullRequest,
        on_delete=models.CASCADE,
        related_name="review_inline_comments",
    )
    parent_review_event = models.ForeignKey(
        PRTimelineEvent,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inline_comments",
    )
    github_node_id = models.CharField(max_length=255, unique=True)
    review_node_id = models.CharField(max_length=255, db_index=True)
    author_login = models.CharField(max_length=255, blank=True)
    # GitHub-side ``createdAt`` of the inline comment (not the row insert time;
    # that's ``created_at`` from ``TimestampedModel``).
    gh_created_at = models.DateTimeField()
    path = models.CharField(max_length=512)
    line = models.IntegerField(null=True, blank=True)
    original_line = models.IntegerField(null=True, blank=True)
    reply_to_node_id = models.CharField(max_length=255, null=True, blank=True)
    thread_root_node_id = models.CharField(max_length=255, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["pull_request", "gh_created_at"], name="syncer_prric_pr_time_idx"),
        ]
        ordering = ["pull_request", "gh_created_at", "id"]

    def __str__(self) -> str:  # pragma: no cover - simple representation
        return f"{self.pull_request} inline @ {self.gh_created_at} ({self.path}:{self.line or '?'})"


class PRReviewInlineCommentBackfill(TimestampedModel):
    """Tracking row for reviews whose nested ``comments(first: K)`` paged.

    Inserted by the syncer when ``PullRequestReview.comments`` returns
    ``pageInfo.hasNextPage = true`` — i.e. we captured the first K inline
    comments under that review and know there are more. Consumed by a v3
    recovery sweep that paginates the rest. Deleted (or marked complete) once
    pagination finishes.

    Single-table scan (``SELECT * FROM PRReviewInlineCommentBackfill``) is the
    operator's hot path for "what's still incomplete?" — keeping the table
    small and dedicated lets the recovery scan be O(rows-needing-work) instead
    of O(reviews) on the much larger ``PRTimelineEvent`` table.

    The pagination cursor / last_attempt_at fields are deliberately omitted
    here; they land in v3 alongside the paginator that uses them.
    """

    review_event = models.OneToOneField(
        PRTimelineEvent,
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="inline_comment_backfill",
    )
    pull_request = models.ForeignKey(
        PullRequest,
        on_delete=models.CASCADE,
        related_name="review_inline_comment_backfills",
    )
    review_node_id = models.CharField(max_length=255, db_index=True)
    # Snapshot of ``comments.totalCount`` at ingest time. Stored on the
    # parent event row too (``PRTimelineEvent.inline_comment_total_count``);
    # duplicating here keeps the recovery scan self-contained.
    total_count = models.IntegerField()

    class Meta:
        indexes = [
            models.Index(fields=["pull_request"], name="syncer_prricbf_pr_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - simple representation
        return f"backfill review={self.review_node_id} total={self.total_count}"
