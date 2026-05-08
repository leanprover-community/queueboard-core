"""Ingest nested ``PullRequestReview.comments`` into the new inline-comment tables.

Sibling of ``timeline_sync.py``: where that module turns ``timelineItems``
nodes into ``PRTimelineEvent`` rows, this one turns the inline-comment
connection nested inside each ``PullRequestReview`` GraphQL node into
``PRReviewInlineComment`` rows + a ``PRReviewInlineCommentBackfill`` row
when the connection had more pages than we fetched.

Input shape (per review group)
- ``review_node_id``: GraphQL ``PullRequestReview.id`` of the parent review.
- ``parent_review_event``: the persisted ``PRTimelineEvent`` row for that
  review submission, or None when no such row exists yet (e.g., a dismissed
  review whose ``ReviewDismissedEvent`` had ``review: null`` so synthesis
  couldn't fire). The durable link is always ``review_node_id``.
- ``total_count``: ``comments.totalCount`` for this review (also stamped on
  ``PRTimelineEvent.inline_comment_total_count``).
- ``has_next_page``: ``comments.pageInfo.hasNextPage``. When True we record
  a ``PRReviewInlineCommentBackfill`` row keyed on ``review_node_id`` so the
  v3 paginator can find it via a tiny dedicated-table scan.
- ``comment_nodes``: raw list of ``PullRequestReviewComment`` GraphQL nodes,
  each shaped like::

      {
          "id": str,                  # github_node_id
          "createdAt": str,           # ISO timestamp
          "path": str,
          "line": int | None,
          "originalLine": int | None,
          "replyTo": {"id": str} | None,
          "author": {"login": str} | None,
      }

Threading (DB-aware walk + monotone UPSERT)
- ``thread_root_node_id`` is computed by walking ``replyTo`` through the
  union of (a) the in-flight set (other comments under any review in the
  current call) and (b) existing ``PRReviewInlineComment`` rows in the DB.
  When a comment's ``replyTo`` target leaves the in-flight set we look it
  up in the DB and copy its already-resolved ``thread_root_node_id``
  (transitively correct, since that row was resolved by a prior ingest).
  If the chain leaves both, we fall back to the immediate ``replyTo`` id —
  best-effort, tightened on a future rewalk that has more of the chain.
- Re-ingest semantics are monotone-toward-truth: a row whose new walk
  reached a definitive root (no fallback) UPSERTs ``thread_root_node_id``;
  a row whose new walk fell back uses INSERT-IGNORE so a possibly-better
  existing value is preserved. This way a wider-context rewalk improves
  the stored root, but a narrower rewalk never regresses it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable

from dateutil import parser as dtparser
from django.utils import timezone

from syncer.models.pr_review_inline_comment import (
    PRReviewInlineComment,
    PRReviewInlineCommentBackfill,
)
from syncer.models.pr_timeline_event import PRTimelineEvent
from syncer.models.pull_request import PullRequest


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReviewInlineCommentsGroup:
    """One ``PullRequestReview``'s nested ``comments`` connection, parsed.

    ``parent_review_event`` is None when the parent timeline event hasn't
    been persisted yet (rare; ingestion phases normally persist reviews
    first, then comments). The FK is recorded as null in that case and the
    row stays linked via ``review_node_id``.
    """

    review_node_id: str
    parent_review_event: PRTimelineEvent | None
    total_count: int
    has_next_page: bool
    comment_nodes: tuple[dict[str, Any], ...]


@dataclass
class InlineCommentsSyncResult:
    comments_created: int = 0
    comments_skipped: int = 0
    backfill_rows_upserted: int = 0
    thread_root_outside_bundle: int = 0


def _parse_iso(value: str | None):
    if not value:
        return None
    dt = dtparser.isoparse(value)
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    return dt


def _author_login(node: dict[str, Any]) -> str:
    """Extract login or empty string for null/deleted authors.

    Mirrors the convention on ``PRTimelineEvent.actor_login``: empty string
    when GitHub returns ``null`` (deleted account) or omits the field.
    Mannequins still expose ``login`` and are therefore captured.
    """
    author = node.get("author")
    if not isinstance(author, dict):
        return ""
    login = author.get("login")
    return str(login) if login else ""


def _build_replyto_map(reviews: Iterable[ReviewInlineCommentsGroup]) -> dict[str, str | None]:
    """Build ``{comment_node_id: reply_to_node_id_or_None}`` across all reviews in the bundle."""
    mapping: dict[str, str | None] = {}
    for group in reviews:
        for node in group.comment_nodes:
            if not isinstance(node, dict):
                continue
            node_id = node.get("id")
            if not node_id:
                continue
            reply_to = node.get("replyTo")
            reply_to_id = reply_to.get("id") if isinstance(reply_to, dict) else None
            mapping[str(node_id)] = str(reply_to_id) if reply_to_id else None
    return mapping


def _compute_thread_root(
    node_id: str,
    replyto_map: dict[str, str | None],
    db_thread_roots: dict[str, str],
) -> tuple[str, bool]:
    """Walk ``replyTo`` from ``node_id`` through the in-flight + DB sets.

    The walk steps through the in-flight ``replyto_map`` first (cheap, no
    DB hit). When a step lands on a ``replyTo`` target that's outside the
    in-flight set, ``db_thread_roots`` (a one-shot prefetch of existing
    rows' ``thread_root_node_id``) is consulted: if the target exists in
    the DB, its already-resolved root is the answer (transitively
    correct). If neither set has the target, fall back to the immediate
    ``replyTo`` id as a best-effort root.

    Returns ``(thread_root_node_id, fell_back)``. ``fell_back`` is True
    iff we exited the chain via the last branch — used by the caller to
    decide between UPSERT (definitive root: safe to overwrite) and
    INSERT-IGNORE (preserve any already-better stored value).
    """
    visited: set[str] = set()
    current = node_id
    while True:
        if current in visited:
            # Cycle (not expected from GitHub but stay defensive).
            return current, False
        visited.add(current)
        reply_to = replyto_map.get(current)
        if reply_to is None:
            # Either ``current`` has no replyTo (it's a thread root), or
            # ``current`` itself isn't in the map. Either way ``current``
            # is the answer; the walk reached a definitive root.
            return current, False
        if reply_to in replyto_map:
            current = reply_to
            continue
        # ``reply_to`` is outside the in-flight set. Consult the DB: if
        # we've previously resolved this comment's chain to a root, copy
        # that root as our answer (transitively correct).
        db_root = db_thread_roots.get(reply_to)
        if db_root is not None:
            return db_root, False
        # Neither in flight nor in DB — fall back to the immediate target.
        return reply_to, True


def sync_review_inline_comments_bundle(
    *,
    pull_request: PullRequest,
    reviews: Iterable[ReviewInlineCommentsGroup],
) -> InlineCommentsSyncResult:
    """Persist inline comments and backfill markers for a bundle of reviews."""
    reviews_list = list(reviews)
    result = InlineCommentsSyncResult()
    if not reviews_list:
        return result

    replyto_map = _build_replyto_map(reviews_list)

    # DB-aware thread-root resolution: prefetch existing rows for any
    # replyTo target that's not in the in-flight set. One batch query
    # bounded by the number of distinct external replyTo ids.
    external_replyto_ids = {reply_to for reply_to in replyto_map.values() if reply_to is not None and reply_to not in replyto_map}
    db_thread_roots: dict[str, str] = {}
    if external_replyto_ids:
        db_thread_roots = dict(
            PRReviewInlineComment.objects.filter(github_node_id__in=external_replyto_ids).values_list(
                "github_node_id", "thread_root_node_id"
            )
        )

    rows: list[PRReviewInlineComment] = []
    fell_back_flags: list[bool] = []
    for group in reviews_list:
        parent_event_id = group.parent_review_event.pk if group.parent_review_event else None
        for node in group.comment_nodes:
            if not isinstance(node, dict):
                result.comments_skipped += 1
                continue
            node_id = node.get("id")
            gh_created_at = _parse_iso(node.get("createdAt"))
            path = node.get("path")
            if not node_id or gh_created_at is None or not path:
                # Required fields missing: skip the row but record it.
                result.comments_skipped += 1
                continue

            reply_to_obj = node.get("replyTo")
            reply_to_id = reply_to_obj.get("id") if isinstance(reply_to_obj, dict) else None
            thread_root, fell_back = _compute_thread_root(str(node_id), replyto_map, db_thread_roots)
            if fell_back:
                result.thread_root_outside_bundle += 1

            rows.append(
                PRReviewInlineComment(
                    pull_request_id=pull_request.pk,
                    parent_review_event_id=parent_event_id,
                    github_node_id=str(node_id),
                    review_node_id=group.review_node_id,
                    author_login=_author_login(node),
                    gh_created_at=gh_created_at,
                    path=str(path),
                    line=node.get("line"),
                    original_line=node.get("originalLine"),
                    reply_to_node_id=str(reply_to_id) if reply_to_id else None,
                    thread_root_node_id=thread_root,
                )
            )
            fell_back_flags.append(fell_back)

    if rows:
        # ``comments_created`` counts only genuinely-new rows. We compute
        # this BEFORE issuing the upsert so we can distinguish "row was
        # inserted" from "row was already present and possibly updated".
        existing_ids = set(
            PRReviewInlineComment.objects.filter(github_node_id__in=[r.github_node_id for r in rows]).values_list(
                "github_node_id", flat=True
            )
        )
        result.comments_created = sum(1 for r in rows if r.github_node_id not in existing_ids)

        # Split rows by walk completeness:
        # - definitive (fell_back=False): UPSERT thread_root_node_id —
        #   safe to overwrite, since the new walk reached a true root.
        # - fallback (fell_back=True): INSERT-IGNORE — preserve any
        #   existing thread_root from a prior ingest that may have had
        #   wider context. Monotone-toward-truth.
        complete_rows = [r for r, fb in zip(rows, fell_back_flags) if not fb]
        fallback_rows = [r for r, fb in zip(rows, fell_back_flags) if fb]
        if complete_rows:
            PRReviewInlineComment.objects.bulk_create(
                complete_rows,
                update_conflicts=True,
                update_fields=["thread_root_node_id"],
                unique_fields=["github_node_id"],
            )
        if fallback_rows:
            PRReviewInlineComment.objects.bulk_create(fallback_rows, ignore_conflicts=True)

    # Backfill markers: one per review where the nested comments connection
    # had more pages than we captured. Keyed on ``review_node_id`` (the
    # durable identifier) so we can track this even when the parent
    # ``PRTimelineEvent`` row doesn't exist (e.g., dismissed reviews whose
    # dismiss event had ``review: null`` on GitHub).
    for group in reviews_list:
        if not group.has_next_page:
            continue
        PRReviewInlineCommentBackfill.objects.update_or_create(
            review_node_id=group.review_node_id,
            defaults={
                "pull_request": pull_request,
                "review_event": group.parent_review_event,
                "total_count": int(group.total_count),
            },
        )
        result.backfill_rows_upserted += 1

    return result


def parse_review_inline_comments_group(
    *,
    review_node_id: str,
    parent_review_event: PRTimelineEvent | None,
    comments_connection: dict[str, Any] | None,
) -> ReviewInlineCommentsGroup | None:
    """Helper for callers parsing a raw GraphQL ``comments`` connection dict.

    Returns ``None`` if the connection is missing or malformed.
    """
    if not isinstance(comments_connection, dict):
        return None
    nodes = comments_connection.get("nodes")
    if not isinstance(nodes, list):
        nodes = []
    page_info = comments_connection.get("pageInfo") or {}
    has_next = bool(page_info.get("hasNextPage"))
    total = int(comments_connection.get("totalCount") or 0)
    return ReviewInlineCommentsGroup(
        review_node_id=str(review_node_id),
        parent_review_event=parent_review_event,
        total_count=total,
        has_next_page=has_next,
        comment_nodes=tuple(n for n in nodes if isinstance(n, dict)),
    )


# ``field`` is imported by the dataclass module above. Re-exporting only the
# narrow helpers callers need keeps the import surface tidy.
__all__ = [
    "ReviewInlineCommentsGroup",
    "InlineCommentsSyncResult",
    "sync_review_inline_comments_bundle",
    "parse_review_inline_comments_group",
]
