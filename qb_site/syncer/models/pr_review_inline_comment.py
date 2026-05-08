from __future__ import annotations

from django.db import models
from django.db.models import Q

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
      ``unique=True`` is enough to support per-row UPSERT semantics on
      re-ingest.

    Linkage
    - ``review_node_id`` is the durable link to the parent
      ``PullRequestReview``. Use this — not ``parent_review_event_id`` —
      as the canonical join key in analytics queries: see the warning on
      ``parent_review_event`` below.
    - ``parent_review_event`` is the convenience FK for ORM joins. Under
      design doc 044's synthesis logic, this should be non-null for every
      inline comment whose parent review has any meaningful state
      (APPROVED / CHANGES_REQUESTED / COMMENTED — including dismissed
      reviews, where the parent row is synthesized from the
      ``REVIEW_DISMISSED`` event's denormalized ``previousReviewState``).
      It can still legitimately be null in narrow cases:

      1. The dismiss event's ``review`` field was null on GitHub (the
         dismissed review was hard-deleted). We can't synthesize a
         parent without the review's identity.
      2. A transient gap during ingest before the synthesis migration
         backfill catches up.

      Joining via ``parent_review_event_id`` will silently exclude
      these. Prefer ``review_node_id`` joined to
      ``PRTimelineEvent.github_node_id`` for analytics that must not
      drop rows.

    Threading
    - ``thread_root_node_id`` is the root of the ``replyTo`` chain. The
      ingest-time walk consults both the in-flight set (other comments
      in the same page) and existing ``PRReviewInlineComment`` rows in
      the DB, so cross-page threads converge to the true root after the
      ancestor chain has been ingested. UPSERT semantics on re-ingest
      mean a later sync that has more of the chain in scope can tighten
      a previously-best-effort root.
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
    reply_to_node_id = models.CharField(max_length=255, null=True, blank=True, db_index=True)
    thread_root_node_id = models.CharField(max_length=255, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["pull_request", "gh_created_at"], name="syncer_prric_pr_time_idx"),
        ]
        constraints = [
            # The durable identifier must actually identify a review: empty
            # string here would silently orphan the comment from the
            # review_node_id-based join path that analytics rely on.
            models.CheckConstraint(
                name="syncer_prric_review_node_id_not_empty",
                condition=~Q(review_node_id=""),
            ),
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

    Linkage
    - ``review_node_id`` is the durable identifier and the unique key for
      this table. ``review_event`` is a convenience FK that may be null for
      reviews that have no corresponding ``PRTimelineEvent`` row — at the
      moment, this only happens for dismissed reviews whose dismiss event
      had ``review: null`` on GitHub (so synthesis couldn't fire). Joining
      via ``review_event_id`` will silently exclude those rows; analytics
      that must not lose anything should join via ``review_node_id`` to
      ``PRTimelineEvent.github_node_id``.
    """

    review_event = models.OneToOneField(
        PRTimelineEvent,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="inline_comment_backfill",
    )
    pull_request = models.ForeignKey(
        PullRequest,
        on_delete=models.CASCADE,
        related_name="review_inline_comment_backfills",
    )
    review_node_id = models.CharField(max_length=255, unique=True)
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
