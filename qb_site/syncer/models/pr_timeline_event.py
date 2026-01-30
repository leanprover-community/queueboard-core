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
    # Present only for HEAD_FORCE_PUSHED events; Git commit SHAs (40 chars)
    before_sha = models.CharField(max_length=40, null=True, blank=True)
    after_sha = models.CharField(max_length=40, null=True, blank=True)

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
