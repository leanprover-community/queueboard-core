from __future__ import annotations

from django.db.models import OuterRef, Q, QuerySet, Subquery

from syncer.models import PRTimelineEvent, PRTimelineEventType, PullRequest
from core.models import Repository


def inconsistent_open_prs_queryset(repository: Repository) -> QuerySet[PullRequest]:
    """Open PRs whose stored scalar state contradicts our own timeline events.

    Such a row looks fully synced by every freshness metric (recent
    ``last_synced_at``, backfills done), so the staleness-based selections never
    surface it. It arises when a sync reads a GraphQL snapshot whose
    ``timelineItems`` already reflect a transition that the top-level scalars
    (``state`` / ``isDraft``) have not yet caught up to -- most likely during a
    burst of rapid mutations ending in a close.

    Two queue-membership-relevant signals are detected, each via a single indexed
    subquery over ``(pull_request, occurred_at)``:

    - closed-but-open: the latest state-flip event is ``CLOSED`` (a ``CLOSED``
      with no later ``REOPENED``) while ``state`` is still ``open``.
    - draft drift: ``is_draft`` disagrees with the latest
      ``READY_FOR_REVIEW`` / ``CONVERT_TO_DRAFT`` event.

    Restricted to ``state='open'`` rows -- the only ones whose open/draft scalars
    affect queue membership, which also bounds the scan to roughly the live queue
    size. Merged-but-open is intentionally not covered: ``MergedEvent`` is not yet
    ingested into the timeline, so there is no local witness for it.

    Shared by the incomplete-PR backfill (which re-enqueues offenders for a
    self-healing sync) and the convergence snapshot (which counts standing
    divergence). Returns an unordered, unsliced queryset; callers add their own
    ``order_by`` / slicing / ``count()``.
    """
    latest_flip = Subquery(
        PRTimelineEvent.objects.filter(
            pull_request=OuterRef("pk"),
            type__in=[PRTimelineEventType.CLOSED, PRTimelineEventType.REOPENED],
        )
        .order_by("-occurred_at", "-id")
        .values("type")[:1]
    )
    latest_draft = Subquery(
        PRTimelineEvent.objects.filter(
            pull_request=OuterRef("pk"),
            type__in=[PRTimelineEventType.READY_FOR_REVIEW, PRTimelineEventType.CONVERT_TO_DRAFT],
        )
        .order_by("-occurred_at", "-id")
        .values("type")[:1]
    )

    closed_but_open = Q(latest_flip=PRTimelineEventType.CLOSED)
    draft_drift = Q(is_draft=True, latest_draft=PRTimelineEventType.READY_FOR_REVIEW) | Q(
        is_draft=False, latest_draft=PRTimelineEventType.CONVERT_TO_DRAFT
    )

    return (
        PullRequest.objects.filter(repository=repository, state="open")
        .annotate(latest_flip=latest_flip, latest_draft=latest_draft)
        .filter(closed_but_open | draft_drift)
    )
