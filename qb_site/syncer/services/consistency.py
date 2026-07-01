from __future__ import annotations

from django.db.models import OuterRef, Q, QuerySet, Subquery
from django.db.models.functions import Lower

from syncer.models import PRLabel, PRTimelineEvent, PRTimelineEventType, PullRequest
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
    size. Merged-but-open is not detected by a dedicated witness (we do not
    ingest ``MergedEvent``), but it does not slip through: GitHub fires a
    ``ClosedEvent`` alongside the ``MergedEvent`` on merge, and we ingest that,
    so a merged-but-open row matches the closed-but-open branch above and
    self-heals on re-sync (its ``state`` then resolves to ``merged``). Ingesting
    ``MergedEvent`` would only add a separately-labeled metric / defense-in-depth
    against a merge path that emits no ``ClosedEvent`` -- not new recovery
    coverage for queue-stranding.

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


def resurrected_prlabels_queryset(repository: Repository | None = None) -> QuerySet[PRLabel]:
    """PRLabel rows that contradict the PR's own label timeline.

    Returns attachments whose *latest* LABELED/UNLABELED timeline event for that
    label (case-insensitive on ``label_name``) is an ``UNLABELED`` -- i.e. the
    timeline says the label is currently removed, yet a ``PRLabel`` row still
    attaches it.

    This is the fingerprint of the archive importer's additive-only label sync
    (design doc 043) resurrecting a label that the live syncer had already
    detached: the archive snapshot predates the removal, so its ``labels.nodes``
    still lists the label, and ``sync_pr_labels(additive_only=True)`` re-creates
    the ``PRLabel`` with a fresh ``created_at`` (the import time) while never
    seeing the newer ``UNLABELED`` event. Live full-reconcile ingest cannot
    produce this state, so the match set is exactly the resurrected rows.

    Matching is case-insensitive because ``PRTimelineEvent.label_name`` and
    ``LabelDef.name`` both store GitHub's display casing independently. Pass
    ``repository`` to scope the scan; ``None`` covers all repos. Returns an
    unordered, unsliced queryset -- callers add their own ordering/slicing.
    """
    latest_label_event = Subquery(
        PRTimelineEvent.objects.filter(
            pull_request=OuterRef("pull_request_id"),
            type__in=[PRTimelineEventType.LABELED, PRTimelineEventType.UNLABELED],
            label_name__isnull=False,
        )
        .annotate(_lname=Lower("label_name"))
        .filter(_lname=OuterRef("_label_name_lower"))
        .order_by("-occurred_at", "-id")
        .values("type")[:1]
    )
    qs = (
        PRLabel.objects.annotate(_label_name_lower=Lower("label_def__name"))
        .annotate(latest_label_event=latest_label_event)
        .filter(latest_label_event=PRTimelineEventType.UNLABELED)
    )
    if repository is not None:
        qs = qs.filter(pull_request__repository=repository)
    return qs
