from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

from dateutil import parser as dtparser
from django.utils import timezone

from analyzer.services.revisions import mark_pr_revision_dirty_if_earlier
from syncer.models.pr_review_inline_comment import PRReviewInlineComment
from syncer.models.pr_timeline_event import PRActorType, PRTimelineEvent, PRTimelineEventType
from syncer.models.pull_request import PullRequest

logger = logging.getLogger(__name__)

REVISION_DIRTY_EVENT_TYPES = {
    PRTimelineEventType.HEAD_FORCE_PUSHED,
}

# Map PullRequestReview.state → typed PRTimelineEventType. PENDING and DISMISSED
# are intentionally absent: pending reviews are dropped at ingest, and dismissals
# arrive as their own ReviewDismissedEvent timeline item.
_REVIEW_STATE_TO_TYPE = {
    "APPROVED": PRTimelineEventType.REVIEW_APPROVED,
    "CHANGES_REQUESTED": PRTimelineEventType.REVIEW_CHANGES_REQUESTED,
    "COMMENTED": PRTimelineEventType.REVIEW_COMMENTED,
}


@dataclass
class TimelineSyncResult:
    created: int = 0
    updated: int = 0
    deleted: int = 0


def _parse_iso(val: str | None):
    if not val:
        return None
    dt = dtparser.isoparse(val)
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    return dt


def _login_or_empty(actor: Any) -> str:
    """Return ``actor.login`` as string, or ``""`` if missing / null.

    Mirrors the convention used by ``PRReviewInlineComment.author_login``:
    empty string when GitHub returns ``null`` (deleted account) or when the
    field is omitted. ``Mannequin`` and ``Bot`` actors expose ``login`` and
    are captured as-is.
    """
    if not isinstance(actor, dict):
        return ""
    login = actor.get("login")
    return str(login) if login else ""


def _actor_type_or_none(actor: Any) -> Optional[str]:
    """Return ``actor.__typename`` when it is a known account kind, else ``None``.

    The allowed set is derived from ``PRActorType.values`` so the helper cannot
    drift from the model's choices. An unmodelled typename (a future
    ``Organization``, say) is dropped rather than stored raw: ``None`` means
    "unknown", and that is exactly what an unrecognized kind is.
    """
    if not isinstance(actor, dict):
        return None
    tn = actor.get("__typename")
    return tn if tn in PRActorType.values else None


def _actor_node_id_or_none(actor: Any) -> Optional[str]:
    """Return the actor's GraphQL node id, or ``None`` when absent."""
    if not isinstance(actor, dict):
        return None
    nid = actor.get("id")
    return str(nid) if nid else None


def _actor_identity(actor: Any) -> Dict[str, Optional[str]]:
    """Return the ``actor_type`` / ``actor_node_id`` pair for one actor/author.

    Both are ``None`` for a null or absent actor. GitHub returns a null actor
    for a real share of events (workflow-driven label changes especially), so
    this is a normal outcome, not an error path.
    """
    return {
        "actor_type": _actor_type_or_none(actor),
        "actor_node_id": _actor_node_id_or_none(actor),
    }


def _extract_event_fields(ev: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Translate one ``timelineItems.nodes[]`` entry into row fields.

    Returns ``None`` when the event should be skipped (unknown ``__typename``,
    missing required fields, or pending review). Returns a dict with keys
    ``type``, ``occurred_at``, ``github_node_id`` plus optional typed fields.
    """
    typename = ev.get("__typename")
    gid = ev.get("id")
    if not gid:
        return None

    type_map = {
        "LabeledEvent": PRTimelineEventType.LABELED,
        "UnlabeledEvent": PRTimelineEventType.UNLABELED,
        "AssignedEvent": PRTimelineEventType.ASSIGNED,
        "UnassignedEvent": PRTimelineEventType.UNASSIGNED,
        "ReadyForReviewEvent": PRTimelineEventType.READY_FOR_REVIEW,
        "ConvertToDraftEvent": PRTimelineEventType.CONVERT_TO_DRAFT,
        "ReopenedEvent": PRTimelineEventType.REOPENED,
        "ClosedEvent": PRTimelineEventType.CLOSED,
        "HeadRefForcePushedEvent": PRTimelineEventType.HEAD_FORCE_PUSHED,
        "IssueComment": PRTimelineEventType.ISSUE_COMMENTED,
        "ReviewDismissedEvent": PRTimelineEventType.REVIEW_DISMISSED,
        "ReviewRequestedEvent": PRTimelineEventType.REVIEW_REQUESTED,
        "ReviewRequestRemovedEvent": PRTimelineEventType.REVIEW_REQUEST_REMOVED,
    }

    fields: Dict[str, Any] = {"github_node_id": gid}

    # PullRequestReview is special: type comes from `state`, occurred_at from
    # `submittedAt`, and pending reviews (submittedAt=null) are dropped.
    if typename == "PullRequestReview":
        ev_type = _REVIEW_STATE_TO_TYPE.get(str(ev.get("state") or "").upper())
        if ev_type is None:
            return None  # PENDING / DISMISSED / unknown state
        occurred_at = _parse_iso(ev.get("submittedAt"))
        if occurred_at is None:
            return None  # pending review without submittedAt
        comments = ev.get("comments") or {}
        total = comments.get("totalCount")
        fields.update(
            type=ev_type,
            occurred_at=occurred_at,
            actor_login=_login_or_empty(ev.get("author")),
            inline_comment_total_count=int(total) if isinstance(total, int) else 0,
            **_actor_identity(ev.get("author")),
        )
        return fields

    ev_type = type_map.get(typename)
    if ev_type is None:
        return None
    occurred_at = _parse_iso(ev.get("createdAt"))
    if occurred_at is None:
        return None

    fields.update(type=ev_type, occurred_at=occurred_at)

    if typename in ("LabeledEvent", "UnlabeledEvent"):
        fields["label_name"] = (ev.get("label") or {}).get("name")
        fields["actor_login"] = (ev.get("actor") or {}).get("login")
        fields.update(_actor_identity(ev.get("actor")))
    elif typename in ("AssignedEvent", "UnassignedEvent"):
        fields["assignee_login"] = (ev.get("assignee") or {}).get("login")
        fields["actor_login"] = (ev.get("actor") or {}).get("login")
        fields.update(_actor_identity(ev.get("actor")))
    elif typename in ("ReadyForReviewEvent", "ConvertToDraftEvent", "ReopenedEvent", "ClosedEvent"):
        fields["actor_login"] = (ev.get("actor") or {}).get("login")
        fields.update(_actor_identity(ev.get("actor")))
    elif typename == "HeadRefForcePushedEvent":
        before_sha = (ev.get("beforeCommit") or {}).get("oid")
        after_sha = (ev.get("afterCommit") or {}).get("oid")
        # The CHECK constraint syncer_prtl_sha_by_type_ck requires both SHAs
        # to be non-null for HEAD_FORCE_PUSHED rows. Live's pr_bundle.graphql
        # always returns them; the legacy archive fragment used by the
        # backfill importer (design doc 043) does not. Drop the event in that
        # case — the live syncer's timeline backfill will pick up the real
        # event with its SHAs once it reaches the PR.
        if not before_sha or not after_sha:
            return None
        fields["before_sha"] = before_sha
        fields["after_sha"] = after_sha
        fields["actor_login"] = (ev.get("actor") or {}).get("login")
        fields.update(_actor_identity(ev.get("actor")))
    elif typename == "IssueComment":
        fields["actor_login"] = _login_or_empty(ev.get("author"))
        fields.update(_actor_identity(ev.get("author")))
    elif typename == "ReviewDismissedEvent":
        # Actor is the dismisser, NEVER the dismissed review's author. Review
        # may be null when the underlying review has been hard-deleted; in
        # that case omit the dismissed_review_* fields.
        fields["actor_login"] = _login_or_empty(ev.get("actor"))
        fields.update(_actor_identity(ev.get("actor")))
        review = ev.get("review") if isinstance(ev.get("review"), dict) else None
        extra: Dict[str, Any] = {"previous_review_state": ev.get("previousReviewState")}
        if review is not None:
            extra["dismissed_review_node_id"] = review.get("id")
            extra["dismissed_review_author"] = _login_or_empty(review.get("author"))
            extra["dismissed_review_submitted_at"] = review.get("submittedAt")
            # Denormalized so _synthesize_dismissed_review_parent can type the
            # row it materializes; there is no other source for it, since the
            # synthesized row is built entirely from this extra blob.
            extra["dismissed_review_author_type"] = _actor_type_or_none(review.get("author"))
            extra["dismissed_review_author_node_id"] = _actor_node_id_or_none(review.get("author"))
        fields["extra"] = extra
    elif typename in ("ReviewRequestedEvent", "ReviewRequestRemovedEvent"):
        fields["actor_login"] = _login_or_empty(ev.get("actor"))
        fields.update(_actor_identity(ev.get("actor")))
        rr = ev.get("requestedReviewer") or {}
        rr_typename = rr.get("__typename") if isinstance(rr, dict) else None
        if rr_typename == "Team":
            fields["requested_team_slug"] = rr.get("slug") or ""
        elif rr_typename in ("User", "Mannequin", "Bot"):
            fields["requested_reviewer_login"] = rr.get("login") or ""

    return fields


def _synthesize_dismissed_review_parent(pr: PullRequest, dismiss_extra: Dict[str, Any]) -> tuple[PRTimelineEvent | None, bool]:
    """Synthesize the REVIEW_<previousReviewState> row for a dismissed review.

    A dismissed review's original submission state is dropped at the
    ``PullRequestReview`` (state=DISMISSED) ingestion path because that node
    alone doesn't tell us what state the review was in before dismissal. The
    original state IS recoverable from the paired ``ReviewDismissedEvent``'s
    ``previousReviewState`` field, which we already denormalize into
    ``REVIEW_DISMISSED.extra``. This synthesizer creates the corresponding
    ``REVIEW_<previousReviewState>`` row (idempotent on
    ``github_node_id == extra.dismissed_review_node_id``) so that the
    ingested data is consistent regardless of whether the syncer ran
    between submission and dismissal (Case A — REVIEW_<state> already
    present) or only after dismissal (Case B — needs synthesis).

    Without this, two PRs with identical upstream history end up with
    different DB shapes depending on sync timing — a non-deterministic
    correctness property we want to avoid. See design doc 044.

    Returns ``(row, was_created)`` for the synthesized parent, or
    ``(None, False)`` when synthesis isn't possible (extra is missing
    fields, ``previousReviewState`` isn't a submission state, or
    ``dismissed_review_submitted_at`` failed to parse).

    As a side effect, when synthesis succeeds and the row was created,
    any existing ``PRReviewInlineComment`` rows with this review's
    ``review_node_id`` and a null ``parent_review_event_id`` are linked
    to the new parent. This handles the ordering "inline comments
    ingested before the dismiss event was seen on a different page".
    """
    review_node_id = dismiss_extra.get("dismissed_review_node_id")
    previous_state = dismiss_extra.get("previous_review_state")
    submitted_at_iso = dismiss_extra.get("dismissed_review_submitted_at")
    author = dismiss_extra.get("dismissed_review_author") or ""
    # Present only when the dismiss event was ingested under code that
    # denormalizes them (design doc 051). Rows whose `extra` predates those
    # keys synthesize with a null type/node id and are healed by the
    # nodes(ids:) backfill instead.
    author_type = dismiss_extra.get("dismissed_review_author_type") or None
    author_node_id = dismiss_extra.get("dismissed_review_author_node_id") or None

    if not review_node_id or not previous_state or not submitted_at_iso:
        return (None, False)

    ev_type = _REVIEW_STATE_TO_TYPE.get(str(previous_state).upper())
    if ev_type is None:
        # PENDING / DISMISSED / unexpected. The first two shouldn't be possible
        # (you can't dismiss a pending or already-dismissed review), but log
        # if we ever see one.
        logger.warning(
            "timeline_sync.skip_synthesis_unexpected_previous_state pr_id=%s review_node_id=%s previous_state=%s",
            pr.pk,
            review_node_id,
            previous_state,
        )
        return (None, False)

    occurred_at = _parse_iso(submitted_at_iso)
    if occurred_at is None:
        logger.warning(
            "timeline_sync.skip_synthesis_unparseable_submitted_at pr_id=%s review_node_id=%s submitted_at=%s",
            pr.pk,
            review_node_id,
            submitted_at_iso,
        )
        return (None, False)

    obj, was_created = PRTimelineEvent.objects.get_or_create(
        pull_request=pr,
        github_node_id=review_node_id,
        defaults={
            "type": ev_type,
            "occurred_at": occurred_at,
            "actor_login": str(author),
            "actor_type": author_type if author_type in PRActorType.values else None,
            "actor_node_id": str(author_node_id) if author_node_id else None,
            # inline_comment_total_count starts NULL: we don't know the count
            # without seeing the actual PullRequestReview node. The CHECK
            # constraint allows null on review-submission types. If a later
            # walk surfaces the PullRequestReview node (in any state), the
            # update path in sync_timeline_events refreshes the count.
        },
    )

    if was_created:
        # Back-link any inline comments that were ingested before this
        # synthesis ran (e.g., the inline comments were on an earlier page
        # than the dismiss event in the back-walk).
        PRReviewInlineComment.objects.filter(
            pull_request=pr,
            review_node_id=review_node_id,
            parent_review_event__isnull=True,
        ).update(parent_review_event=obj)

    return (obj, was_created)


def sync_timeline_events(
    pr: PullRequest,
    events: Iterable[Dict[str, Any]],
    *,
    archive_mode: bool = False,
) -> TimelineSyncResult:
    """Insert key timeline events for a PR using GraphQL ids (idempotent).

    Each ``PRTimelineEvent`` row corresponds 1:1 to one ``timelineItems`` node;
    inline review comments live in ``PRReviewInlineComment`` and are written by
    the inline-comments service, not here.

    Recognized ``__typename`` values:

    v1 (revision-impacting and structural):
      - LabeledEvent, UnlabeledEvent (with ``label_name``)
      - AssignedEvent, UnassignedEvent (with ``assignee_login``)
      - ReadyForReviewEvent, ConvertToDraftEvent, ReopenedEvent, ClosedEvent
      - HeadRefForcePushedEvent (with ``before_sha`` / ``after_sha``)

    v2 (review/comment activity, see design doc 044):
      - IssueComment → ISSUE_COMMENTED
      - PullRequestReview → REVIEW_APPROVED / REVIEW_CHANGES_REQUESTED /
        REVIEW_COMMENTED (state-routed; PENDING and DISMISSED dropped here)
        with ``inline_comment_total_count`` from ``comments.totalCount``
      - ReviewDismissedEvent → REVIEW_DISMISSED, with denormalized review
        identity + state in ``extra``. As a side effect of ingesting a
        ``REVIEW_DISMISSED`` row whose ``extra`` carries the dismissed
        review's identity + previous state, this function also synthesizes
        the corresponding ``REVIEW_<previousReviewState>`` row (idempotent
        on the dismissed review's ``github_node_id``) so the ingested data
        is consistent regardless of whether the syncer ran between
        submission and dismissal. See ``_synthesize_dismissed_review_parent``.
      - ReviewRequestedEvent / ReviewRequestRemovedEvent →
        REVIEW_REQUESTED / REVIEW_REQUEST_REMOVED, routed by reviewer kind
        (User/Bot/Mannequin → ``requested_reviewer_login``; Team →
        ``requested_team_slug``)

    Idempotency: ``github_node_id`` is unique; existing rows are updated only
    to fill previously-empty fields. Unknown ``__typename`` values are ignored.

    Archive mode (design doc 043): when ``archive_mode=True``, the dismissed-
    review parent synthesis is skipped. The legacy ``ReviewDismissedEvent``
    fragment in ``src/queueboard/queries/pr_info.graphql`` lacks
    ``previousReviewState``, so ``extra`` cannot carry the data that
    ``_synthesize_dismissed_review_parent`` keys on. The dismiss-event row
    itself is still imported; a later upgrader-driven rewalk under live
    code populates the synthesized parent if the original review still
    exists in GitHub's response. Legacy ``PullRequestReview`` nodes
    without ``state``/``submittedAt`` are dropped by
    ``_extract_event_fields`` regardless of mode.
    """
    created = 0
    updated = 0
    reset_commits_backfill = False
    earliest_new_ts = None

    for ev in events:
        if not isinstance(ev, dict):
            continue
        fields = _extract_event_fields(ev)
        if fields is None:
            continue

        gid = fields.pop("github_node_id")
        ev_type = fields["type"]
        occurred_at = fields["occurred_at"]
        obj, was_created = PRTimelineEvent.objects.get_or_create(
            pull_request=pr,
            github_node_id=gid,
            defaults=fields,
        )
        if was_created:
            created += 1
            if ev_type in REVISION_DIRTY_EVENT_TYPES and (earliest_new_ts is None or occurred_at < earliest_new_ts):
                earliest_new_ts = occurred_at
            if ev_type == PRTimelineEventType.HEAD_FORCE_PUSHED:
                reset_commits_backfill = True
        else:
            update_fields: list[str] = []
            for col in (
                "label_name",
                "assignee_login",
                "actor_login",
                "actor_type",
                "actor_node_id",
                "before_sha",
                "after_sha",
                "requested_reviewer_login",
                "requested_team_slug",
            ):
                new_val = fields.get(col)
                if new_val and not getattr(obj, col):
                    setattr(obj, col, new_val)
                    update_fields.append(col)
            # inline_comment_total_count is a real GitHub-truth count: refresh
            # whenever the bundle gives us a non-null value, even if the row
            # already had one (a later sync may see a higher count).
            new_total = fields.get("inline_comment_total_count")
            if new_total is not None and obj.inline_comment_total_count != new_total:
                obj.inline_comment_total_count = new_total
                update_fields.append("inline_comment_total_count")
            # `extra` is JSON: only fill when previously empty to keep this
            # update path append-only on existing rows.
            new_extra = fields.get("extra")
            if isinstance(new_extra, dict) and new_extra and not obj.extra:
                obj.extra = new_extra
                update_fields.append("extra")
            if update_fields:
                obj.save(update_fields=update_fields)
                updated += 1

        # Synthesize the dismissed review's parent row regardless of whether
        # the dismiss event row was just created or already existed. Running
        # on every ingest (not just first creation) makes the live code
        # self-healing for any production rows whose parents were missed by
        # the migration's backfill — synthesis is idempotent on the dismissed
        # review's github_node_id, so re-running is one extra SELECT.
        # Archive mode skips synthesis: the legacy fragment lacks
        # previousReviewState so the synthesizer has nothing to key on.
        if not archive_mode and ev_type == PRTimelineEventType.REVIEW_DISMISSED:
            extra_dict = fields.get("extra") or obj.extra or {}
            if isinstance(extra_dict, dict):
                _, synth_created = _synthesize_dismissed_review_parent(pr, extra_dict)
                if synth_created:
                    created += 1

    if reset_commits_backfill:
        pr.commits_backfill_done = False
        pr.commits_backfill_cursor = None
        pr.commits_earliest_synced_at = None
        pr.save(update_fields=["commits_backfill_done", "commits_backfill_cursor", "commits_earliest_synced_at"])

    if earliest_new_ts:
        mark_pr_revision_dirty_if_earlier(pr, earliest_new_ts)

    return TimelineSyncResult(created=created, updated=updated, deleted=0)
