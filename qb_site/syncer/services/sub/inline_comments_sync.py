"""Ingest nested ``PullRequestReview.comments`` into the new inline-comment tables.

Sibling of ``timeline_sync.py``: where that module turns ``timelineItems``
nodes into ``PRTimelineEvent`` rows, this one turns the inline-comment
connection nested inside each ``PullRequestReview`` GraphQL node into
``PRReviewInlineComment`` rows + a ``PRReviewInlineCommentBackfill`` row
when the connection had more pages than we fetched.

Wired into the bundle ingest path in Chunk 4 (design doc 044). Until then
this module is importable but has no callers.

Input shape (per review group)
- ``review_node_id``: GraphQL ``PullRequestReview.id`` of the parent review.
- ``parent_review_event``: the persisted ``PRTimelineEvent`` row for that
  review submission (or None if not yet persisted; FK is left null and the
  durable link via ``review_node_id`` carries the relationship).
- ``total_count``: ``comments.totalCount`` for this review (also stamped on
  ``PRTimelineEvent.inline_comment_total_count`` by Chunk 4).
- ``has_next_page``: ``comments.pageInfo.hasNextPage``. When True we record
  a ``PRReviewInlineCommentBackfill`` row; the v3 paginator consumes it.
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

Threading
- ``thread_root_node_id`` is computed by walking ``replyTo`` within the
  in-flight set (the union of all comments across all reviews in the
  bundle). If the chain stays in-bundle, the topmost in-bundle node is the
  root. If a comment's ``replyTo`` target sits outside the bundle, the
  immediate ``replyTo`` id is used as the root — best-effort, reconciled
  on later rewalks. A self-rooted comment (no ``replyTo``) is its own root.
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
) -> tuple[str, bool]:
    """Walk ``replyTo`` from ``node_id`` within the in-flight map.

    Returns ``(thread_root_node_id, fell_back_outside_bundle)``. The boolean
    is True iff the chain crossed out of the in-flight set and we used the
    immediate ``replyTo`` target as the root.
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
            # ``current`` itself isn't in the map (caller bug — current
            # always starts in the map and we only step to in-map nodes
            # in the branch below). Either way ``current`` is the answer.
            return current, False
        if reply_to not in replyto_map:
            # Chain leaves the bundle — fall back to the immediate replyTo
            # target as the (best-effort) root.
            return reply_to, True
        current = reply_to


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

    rows: list[PRReviewInlineComment] = []
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
            thread_root, fell_back = _compute_thread_root(str(node_id), replyto_map)
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

    if rows:
        # ``ignore_conflicts=True`` makes re-ingest a no-op against the
        # ``github_node_id`` unique constraint. ``bulk_create`` returns the
        # full input list with ``pk`` populated only for newly inserted
        # rows in some backends; we trust the unique constraint instead of
        # post-hoc inspection and count by counting the rows we produced
        # for which a matching node id did not previously exist.
        existing_ids = set(
            PRReviewInlineComment.objects.filter(github_node_id__in=[r.github_node_id for r in rows]).values_list(
                "github_node_id", flat=True
            )
        )
        new_rows = [r for r in rows if r.github_node_id not in existing_ids]
        PRReviewInlineComment.objects.bulk_create(new_rows, ignore_conflicts=True)
        result.comments_created = len(new_rows)
    else:
        result.comments_created = 0

    # Backfill markers: one per review where the connection had more pages.
    for group in reviews_list:
        if not group.has_next_page:
            continue
        if group.parent_review_event is None:
            # Without a parent FK we have no anchor for the OneToOne row.
            # This shouldn't happen on the normal ingest path; log so the
            # case is visible if it ever fires.
            logger.warning(
                "inline_comments_sync.skipped_backfill_no_parent_event review_node_id=%s pull_request_id=%s",
                group.review_node_id,
                pull_request.pk,
            )
            continue
        _, _ = PRReviewInlineCommentBackfill.objects.update_or_create(
            review_event=group.parent_review_event,
            defaults={
                "pull_request": pull_request,
                "review_node_id": group.review_node_id,
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
