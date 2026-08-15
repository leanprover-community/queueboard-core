from __future__ import annotations

from django.db import models
from django.db.models import Q

from core.models.base import TimestampedModel
from .pull_request import PullRequest


class PRTimelineEventType(models.TextChoices):
    LABELED = "LABELED", "labeled"
    UNLABELED = "UNLABELED", "unlabeled"
    ASSIGNED = "ASSIGNED", "assigned"
    UNASSIGNED = "UNASSIGNED", "unassigned"
    READY_FOR_REVIEW = "READY_FOR_REVIEW", "ready_for_review"
    CONVERT_TO_DRAFT = "CONVERT_TO_DRAFT", "convert_to_draft"
    REOPENED = "REOPENED", "reopened"
    CLOSED = "CLOSED", "closed"
    HEAD_FORCE_PUSHED = "HEAD_FORCE_PUSHED", "head_force_pushed"
    ISSUE_COMMENTED = "ISSUE_COMMENTED", "issue_commented"
    REVIEW_APPROVED = "REVIEW_APPROVED", "review_approved"
    REVIEW_CHANGES_REQUESTED = "REVIEW_CHANGES_REQUESTED", "review_changes_requested"
    REVIEW_COMMENTED = "REVIEW_COMMENTED", "review_commented"
    REVIEW_DISMISSED = "REVIEW_DISMISSED", "review_dismissed"
    REVIEW_REQUESTED = "REVIEW_REQUESTED", "review_requested"
    REVIEW_REQUEST_REMOVED = "REVIEW_REQUEST_REMOVED", "review_request_removed"


class PRActorType(models.TextChoices):
    """GraphQL ``__typename`` of a timeline actor account.

    Values are GitHub's exact wire casing so they compare directly against
    ``__typename`` (the ``requested_*`` routing in ``timeline_sync`` already
    compares raw typenames).
    """

    USER = "User", "user"
    BOT = "Bot", "bot"
    MANNEQUIN = "Mannequin", "mannequin"


class PRTimelineEvent(TimestampedModel):
    """Key timeline events for a PR used in status evolution analytics.

    Stored event kinds (v1):
    - Label add/remove (LABELED/UNLABELED) with ``label_name``
    - Draft toggles (READY_FOR_REVIEW/CONVERT_TO_DRAFT)
    - State flips (REOPENED/CLOSED)
    - Force push (HEAD_FORCE_PUSHED) with ``before_sha`` and ``after_sha``

    Idempotency & indexes
    - ``github_node_id`` is the GraphQL timeline item id when available and is conditionally unique.
    - Index on ``(pull_request, occurred_at)`` supports efficient replay in chronological order.
    - We purposefully keep ``label_name`` as a plain string to preserve historical names without
      requiring a join to the label catalog (LabelDef) and to keep ingestion fast.
    """

    pull_request = models.ForeignKey(PullRequest, on_delete=models.CASCADE, related_name="timeline_events")
    github_node_id = models.CharField(max_length=255, null=True, blank=True)
    type = models.CharField(max_length=32, choices=PRTimelineEventType.choices)
    occurred_at = models.DateTimeField()
    # Present only for LABELED/UNLABELED events; stored as-is from GitHub (display casing).
    label_name = models.CharField(max_length=100, null=True, blank=True)
    # Present only for ASSIGNED/UNASSIGNED events (assignee login, display casing).
    assignee_login = models.CharField(max_length=255, null=True, blank=True)
    # Present for ASSIGNED/UNASSIGNED events when available.
    actor_login = models.CharField(max_length=255, null=True, blank=True)
    # GraphQL __typename of the acting account, as returned by the timeline
    # queries. NULL means *unknown*, never "User": rows ingested before this
    # column existed, archive-imported rows whose legacy fragment omits the
    # actor entirely, and events where GitHub itself returns a null actor
    # (workflow-driven label events routinely do) all land here.
    # Note "Bot" identifies a GitHub App; machine accounts that are ordinary
    # user accounts report "User", so this is necessary but not sufficient for
    # "was this automation?".
    actor_type = models.CharField(max_length=16, choices=PRActorType.choices, null=True, blank=True)
    # GraphQL node id of the acting account. Stable across login renames, so
    # downstream automation lists should key on this rather than actor_login.
    # NULL under the same conditions as actor_type.
    actor_node_id = models.CharField(max_length=255, null=True, blank=True)
    # Present only for HEAD_FORCE_PUSHED events; Git commit SHAs (40 chars)
    before_sha = models.CharField(max_length=40, null=True, blank=True)
    after_sha = models.CharField(max_length=40, null=True, blank=True)
    # Display-time denormalization for review/dismissal events (e.g. dismissed
    # review identity for REVIEW_DISMISSED). Read with the row, never filtered
    # on; query-hot fields are promoted to typed columns instead.
    extra = models.JSONField(default=dict, blank=True)
    # Populated for REVIEW_REQUESTED / REVIEW_REQUEST_REMOVED when the target
    # is a User or Mannequin. Mutually exclusive with requested_team_slug.
    requested_reviewer_login = models.CharField(max_length=255, null=True, blank=True, db_index=True)
    # Populated for REVIEW_REQUESTED / REVIEW_REQUEST_REMOVED when the target
    # is a Team. Mutually exclusive with requested_reviewer_login.
    requested_team_slug = models.CharField(max_length=255, null=True, blank=True, db_index=True)
    # Populated for REVIEW_APPROVED / REVIEW_CHANGES_REQUESTED / REVIEW_COMMENTED
    # from comments.totalCount on the PullRequestReview node. Real GitHub-truth
    # value, not sync-state — used to detect reviews whose inline comments
    # exceeded the per-review fetch limit (see PRReviewInlineCommentBackfill).
    inline_comment_total_count = models.IntegerField(null=True, blank=True)
    # Provenance: set when the row was first created by the archive backfill
    # importer (design doc 043). Never touched by live ingestion paths.
    archive_imported_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            # Enforce uniqueness for provided timeline item ids.
            models.UniqueConstraint(
                fields=["github_node_id"],
                name="syncer_prtimelineevent_node_id_unique",
                condition=Q(github_node_id__isnull=False),
            ),
            # Ensure SHA fields are only set on HEAD_FORCE_PUSHED events and are both present there.
            models.CheckConstraint(
                name="syncer_prtl_sha_by_type_ck",
                condition=(
                    (Q(type=PRTimelineEventType.HEAD_FORCE_PUSHED) & Q(before_sha__isnull=False) & Q(after_sha__isnull=False))
                    | (~Q(type=PRTimelineEventType.HEAD_FORCE_PUSHED) & Q(before_sha__isnull=True) & Q(after_sha__isnull=True))
                ),
            ),
            # If label_name is set, the type must be LABELED or UNLABELED.
            models.CheckConstraint(
                name="syncer_prtl_label_by_type_ck",
                condition=(Q(label_name__isnull=True) | Q(type__in=[PRTimelineEventType.LABELED, PRTimelineEventType.UNLABELED])),
            ),
            # If a requested-reviewer column is populated, the type must be a
            # review-request event. Inverse direction (a review-request must
            # always have one of the columns set) is intentionally not enforced
            # — GitHub has historically returned null requestedReviewer for
            # deleted/anonymized targets and we'd rather store the event than
            # crash ingestion.
            models.CheckConstraint(
                name="syncer_prtl_requested_reviewer_by_type_ck",
                condition=(
                    Q(requested_reviewer_login__isnull=True, requested_team_slug__isnull=True)
                    | Q(
                        type__in=[
                            PRTimelineEventType.REVIEW_REQUESTED,
                            PRTimelineEventType.REVIEW_REQUEST_REMOVED,
                        ]
                    )
                ),
            ),
            # At most one of requested_reviewer_login / requested_team_slug is
            # set on any given row. The two columns mirror disjoint members of
            # GraphQL's requestedReviewer union (User/Bot/Mannequin vs Team).
            models.CheckConstraint(
                name="syncer_prtl_requested_reviewer_mutex_ck",
                condition=Q(requested_reviewer_login__isnull=True) | Q(requested_team_slug__isnull=True),
            ),
            # inline_comment_total_count mirrors PullRequestReview.comments.totalCount
            # and is therefore only meaningful on the three submitted-review
            # event types. REVIEW_DISMISSED captures the dismissal event, not
            # the underlying review, so it must remain null.
            models.CheckConstraint(
                name="syncer_prtl_inline_total_by_type_ck",
                condition=(
                    Q(inline_comment_total_count__isnull=True)
                    | Q(
                        type__in=[
                            PRTimelineEventType.REVIEW_APPROVED,
                            PRTimelineEventType.REVIEW_CHANGES_REQUESTED,
                            PRTimelineEventType.REVIEW_COMMENTED,
                        ]
                    )
                ),
            ),
        ]
        indexes = [
            models.Index(fields=["pull_request", "occurred_at"], name="syncer_prtimeline_pr_time_idx"),
            models.Index(
                fields=["pull_request", "after_sha"],
                name="syncer_prtl_aftersha_idx",
                condition=Q(after_sha__isnull=False) & Q(type=PRTimelineEventType.HEAD_FORCE_PUSHED),
            ),
        ]
        ordering = ["pull_request", "occurred_at", "id"]

    def __str__(self) -> str:  # pragma: no cover - simple representation
        extra = f" ({self.label_name})" if self.label_name else ""
        return f"{self.pull_request} @ {self.occurred_at} {self.type}{extra}"
