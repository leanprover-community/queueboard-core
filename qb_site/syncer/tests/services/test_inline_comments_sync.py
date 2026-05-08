"""Tests for ``syncer.services.sub.inline_comments_sync``.

The service is bundle-scoped: thread-root resolution walks the in-flight
``replyTo`` map across *all* reviews in a single ingest, since modern
GitHub wraps each thread reply in its own ``PullRequestReview``. Tests
exercise that contract end-to-end against fixture-shaped GraphQL dicts.
"""

from __future__ import annotations

from typing import Any

from django.test import TestCase
from django.utils import timezone

from syncer.models import (
    PRReviewInlineComment,
    PRReviewInlineCommentBackfill,
    PRTimelineEvent,
)
from syncer.models.pr_timeline_event import PRTimelineEventType
from syncer.services.sub.inline_comments_sync import (
    InlineCommentsSyncResult,
    ReviewInlineCommentsGroup,
    parse_review_inline_comments_group,
    sync_review_inline_comments_bundle,
)
from syncer.tests.factories import make_pr, make_repo


def _comment(
    node_id: str,
    *,
    path: str = "src/foo.py",
    line: int | None = 10,
    original_line: int | None = None,
    reply_to_id: str | None = None,
    author: str | None = "alice",
    created_at: str = "2026-05-01T12:00:00Z",
) -> dict[str, Any]:
    """Return a comment node shaped like the GraphQL response."""
    node: dict[str, Any] = {
        "id": node_id,
        "createdAt": created_at,
        "path": path,
        "line": line,
        "originalLine": original_line,
        "replyTo": {"id": reply_to_id} if reply_to_id else None,
        "author": {"login": author} if author is not None else None,
    }
    return node


def _make_review_event(pr, *, node_id: str) -> PRTimelineEvent:
    """Persist a stand-in ``PRTimelineEvent`` for a review submission.

    Chunk 4 introduces ``REVIEW_*`` event types; until then we re-use an
    existing valid type for testing — the FK linkage is what we exercise
    here, not the event semantics.
    """
    return PRTimelineEvent.objects.create(
        pull_request=pr,
        github_node_id=node_id,
        type=PRTimelineEventType.READY_FOR_REVIEW,
        occurred_at=timezone.now(),
    )


class InlineCommentsSyncBaseTests(TestCase):
    def setUp(self) -> None:
        self.repo = make_repo()
        self.pr = make_pr(self.repo, 1)

    def _group(
        self,
        *,
        review_node_id: str = "REV_1",
        parent_event: PRTimelineEvent | None = None,
        total_count: int | None = None,
        has_next_page: bool = False,
        comments: list[dict[str, Any]] | None = None,
    ) -> ReviewInlineCommentsGroup:
        nodes = comments or []
        return ReviewInlineCommentsGroup(
            review_node_id=review_node_id,
            parent_review_event=parent_event,
            total_count=total_count if total_count is not None else len(nodes),
            has_next_page=has_next_page,
            comment_nodes=tuple(nodes),
        )


class TestEmptyAndShortCircuit(InlineCommentsSyncBaseTests):
    def test_no_reviews_is_a_no_op(self) -> None:
        res = sync_review_inline_comments_bundle(pull_request=self.pr, reviews=[])
        self.assertEqual(res, InlineCommentsSyncResult())
        self.assertFalse(PRReviewInlineComment.objects.exists())
        self.assertFalse(PRReviewInlineCommentBackfill.objects.exists())

    def test_review_with_no_comments_creates_nothing(self) -> None:
        parent = _make_review_event(self.pr, node_id="REV_1")
        res = sync_review_inline_comments_bundle(
            pull_request=self.pr,
            reviews=[self._group(parent_event=parent, comments=[])],
        )
        self.assertEqual(res.comments_created, 0)
        self.assertFalse(PRReviewInlineComment.objects.exists())


class TestRowFields(InlineCommentsSyncBaseTests):
    def test_creates_one_row_per_comment_with_all_fields(self) -> None:
        parent = _make_review_event(self.pr, node_id="REV_1")
        group = self._group(
            parent_event=parent,
            comments=[
                _comment(
                    "C_1",
                    path="src/a.py",
                    line=12,
                    original_line=10,
                    author="alice",
                    created_at="2026-04-01T00:00:00Z",
                ),
            ],
        )
        sync_review_inline_comments_bundle(pull_request=self.pr, reviews=[group])
        row = PRReviewInlineComment.objects.get(github_node_id="C_1")
        self.assertEqual(row.pull_request_id, self.pr.pk)
        self.assertEqual(row.parent_review_event_id, parent.pk)
        self.assertEqual(row.review_node_id, "REV_1")
        self.assertEqual(row.author_login, "alice")
        self.assertEqual(row.path, "src/a.py")
        self.assertEqual(row.line, 12)
        self.assertEqual(row.original_line, 10)
        self.assertIsNone(row.reply_to_node_id)
        self.assertEqual(row.thread_root_node_id, "C_1")
        self.assertEqual(row.gh_created_at.year, 2026)

    def test_null_author_persists_as_empty_string(self) -> None:
        parent = _make_review_event(self.pr, node_id="REV_1")
        group = self._group(
            parent_event=parent,
            comments=[_comment("C_1", author=None)],
        )
        sync_review_inline_comments_bundle(pull_request=self.pr, reviews=[group])
        row = PRReviewInlineComment.objects.get(github_node_id="C_1")
        self.assertEqual(row.author_login, "")

    def test_skips_comment_missing_required_fields(self) -> None:
        parent = _make_review_event(self.pr, node_id="REV_1")
        bad_no_id = _comment("C_OK")
        bad_no_id.pop("id")
        bad_no_path = _comment("C_NO_PATH")
        bad_no_path["path"] = ""
        bad_no_created = _comment("C_NO_CREATED")
        bad_no_created["createdAt"] = ""
        good = _comment("C_GOOD")

        res = sync_review_inline_comments_bundle(
            pull_request=self.pr,
            reviews=[
                self._group(
                    parent_event=parent,
                    comments=[bad_no_id, bad_no_path, bad_no_created, good],
                )
            ],
        )
        self.assertEqual(res.comments_skipped, 3)
        self.assertEqual(res.comments_created, 1)
        ids = list(PRReviewInlineComment.objects.values_list("github_node_id", flat=True))
        self.assertEqual(ids, ["C_GOOD"])

    def test_handles_missing_parent_review_event_gracefully(self) -> None:
        # FK left null; review_node_id still records the linkage.
        group = self._group(parent_event=None, comments=[_comment("C_1")])
        sync_review_inline_comments_bundle(pull_request=self.pr, reviews=[group])
        row = PRReviewInlineComment.objects.get(github_node_id="C_1")
        self.assertIsNone(row.parent_review_event_id)
        self.assertEqual(row.review_node_id, "REV_1")


class TestThreadRoot(InlineCommentsSyncBaseTests):
    def test_top_level_comment_is_its_own_root(self) -> None:
        parent = _make_review_event(self.pr, node_id="REV_1")
        group = self._group(parent_event=parent, comments=[_comment("C_1")])
        sync_review_inline_comments_bundle(pull_request=self.pr, reviews=[group])
        row = PRReviewInlineComment.objects.get(github_node_id="C_1")
        self.assertEqual(row.thread_root_node_id, "C_1")

    def test_walks_chain_within_single_review(self) -> None:
        parent = _make_review_event(self.pr, node_id="REV_1")
        group = self._group(
            parent_event=parent,
            comments=[
                _comment("ROOT"),
                _comment("CHILD", reply_to_id="ROOT"),
                _comment("GRANDCHILD", reply_to_id="CHILD"),
            ],
        )
        sync_review_inline_comments_bundle(pull_request=self.pr, reviews=[group])
        roots = {r.github_node_id: r.thread_root_node_id for r in PRReviewInlineComment.objects.all()}
        self.assertEqual(roots, {"ROOT": "ROOT", "CHILD": "ROOT", "GRANDCHILD": "ROOT"})

    def test_walks_chain_across_reviews_in_bundle(self) -> None:
        # Modern GitHub: each reply is wrapped in its own PullRequestReview.
        # Thread-root resolution must span the bundle.
        parent_root = _make_review_event(self.pr, node_id="REV_ROOT")
        parent_reply1 = _make_review_event(self.pr, node_id="REV_REPLY1")
        parent_reply2 = _make_review_event(self.pr, node_id="REV_REPLY2")
        groups = [
            self._group(
                review_node_id="REV_ROOT",
                parent_event=parent_root,
                comments=[_comment("ROOT")],
            ),
            self._group(
                review_node_id="REV_REPLY1",
                parent_event=parent_reply1,
                comments=[_comment("CHILD", reply_to_id="ROOT")],
            ),
            self._group(
                review_node_id="REV_REPLY2",
                parent_event=parent_reply2,
                comments=[_comment("GRANDCHILD", reply_to_id="CHILD")],
            ),
        ]
        sync_review_inline_comments_bundle(pull_request=self.pr, reviews=groups)
        roots = {r.github_node_id: r.thread_root_node_id for r in PRReviewInlineComment.objects.all()}
        self.assertEqual(roots["GRANDCHILD"], "ROOT")
        self.assertEqual(roots["CHILD"], "ROOT")
        self.assertEqual(roots["ROOT"], "ROOT")

    def test_falls_back_to_replyto_when_target_outside_bundle(self) -> None:
        # The "ROOT" comment isn't included in this bundle; CHILD's replyTo
        # points to it. Falls back to "ROOT" as the thread root.
        parent = _make_review_event(self.pr, node_id="REV_1")
        group = self._group(
            parent_event=parent,
            comments=[_comment("CHILD", reply_to_id="ROOT_OUTSIDE")],
        )
        res = sync_review_inline_comments_bundle(pull_request=self.pr, reviews=[group])
        self.assertEqual(res.thread_root_outside_bundle, 1)
        row = PRReviewInlineComment.objects.get(github_node_id="CHILD")
        self.assertEqual(row.thread_root_node_id, "ROOT_OUTSIDE")
        self.assertEqual(row.reply_to_node_id, "ROOT_OUTSIDE")

    def test_falls_back_when_chain_partially_outside_bundle(self) -> None:
        # CHILD → MID (in bundle) → OUTSIDE (not in bundle)
        # Walking should stop at MID's replyTo and fall back to OUTSIDE.
        parent = _make_review_event(self.pr, node_id="REV_1")
        group = self._group(
            parent_event=parent,
            comments=[
                _comment("MID", reply_to_id="OUTSIDE_ROOT"),
                _comment("CHILD", reply_to_id="MID"),
            ],
        )
        res = sync_review_inline_comments_bundle(pull_request=self.pr, reviews=[group])
        self.assertEqual(res.thread_root_outside_bundle, 2)  # both MID and CHILD fall through
        roots = {r.github_node_id: r.thread_root_node_id for r in PRReviewInlineComment.objects.all()}
        self.assertEqual(roots["MID"], "OUTSIDE_ROOT")
        self.assertEqual(roots["CHILD"], "OUTSIDE_ROOT")

    def test_cycle_in_replyto_does_not_loop_forever(self) -> None:
        # GitHub shouldn't produce a cycle but the dispatcher must be robust.
        parent = _make_review_event(self.pr, node_id="REV_1")
        group = self._group(
            parent_event=parent,
            comments=[
                _comment("A", reply_to_id="B"),
                _comment("B", reply_to_id="A"),
            ],
        )
        sync_review_inline_comments_bundle(pull_request=self.pr, reviews=[group])
        # Both rows should have been written (any non-looping thread_root is fine).
        self.assertEqual(PRReviewInlineComment.objects.count(), 2)


class TestIdempotency(InlineCommentsSyncBaseTests):
    def test_re_ingest_is_a_no_op(self) -> None:
        parent = _make_review_event(self.pr, node_id="REV_1")
        group = self._group(parent_event=parent, comments=[_comment("C_1"), _comment("C_2")])
        first = sync_review_inline_comments_bundle(pull_request=self.pr, reviews=[group])
        second = sync_review_inline_comments_bundle(pull_request=self.pr, reviews=[group])
        self.assertEqual(first.comments_created, 2)
        self.assertEqual(second.comments_created, 0)
        self.assertEqual(PRReviewInlineComment.objects.count(), 2)

    def test_partial_re_ingest_only_inserts_new(self) -> None:
        parent = _make_review_event(self.pr, node_id="REV_1")
        first_group = self._group(parent_event=parent, comments=[_comment("C_1")])
        sync_review_inline_comments_bundle(pull_request=self.pr, reviews=[first_group])

        # Second pass adds C_2 alongside the already-ingested C_1.
        second_group = self._group(parent_event=parent, comments=[_comment("C_1"), _comment("C_2")])
        res = sync_review_inline_comments_bundle(pull_request=self.pr, reviews=[second_group])
        self.assertEqual(res.comments_created, 1)
        self.assertEqual(PRReviewInlineComment.objects.count(), 2)


class TestBackfillTracker(InlineCommentsSyncBaseTests):
    def test_no_backfill_row_when_has_next_page_false(self) -> None:
        parent = _make_review_event(self.pr, node_id="REV_1")
        group = self._group(parent_event=parent, comments=[_comment("C_1")], has_next_page=False)
        res = sync_review_inline_comments_bundle(pull_request=self.pr, reviews=[group])
        self.assertEqual(res.backfill_rows_upserted, 0)
        self.assertFalse(PRReviewInlineCommentBackfill.objects.exists())

    def test_backfill_row_created_when_has_next_page_true(self) -> None:
        parent = _make_review_event(self.pr, node_id="REV_1")
        group = self._group(
            parent_event=parent,
            comments=[_comment("C_1")],
            total_count=42,
            has_next_page=True,
        )
        res = sync_review_inline_comments_bundle(pull_request=self.pr, reviews=[group])
        self.assertEqual(res.backfill_rows_upserted, 1)
        backfill = PRReviewInlineCommentBackfill.objects.get(review_event=parent)
        self.assertEqual(backfill.review_node_id, "REV_1")
        self.assertEqual(backfill.total_count, 42)
        self.assertEqual(backfill.pull_request_id, self.pr.pk)

    def test_backfill_row_total_count_updates_on_re_ingest(self) -> None:
        parent = _make_review_event(self.pr, node_id="REV_1")
        first = self._group(
            parent_event=parent,
            comments=[_comment("C_1")],
            total_count=42,
            has_next_page=True,
        )
        sync_review_inline_comments_bundle(pull_request=self.pr, reviews=[first])
        second = self._group(
            parent_event=parent,
            comments=[_comment("C_1")],
            total_count=99,
            has_next_page=True,
        )
        sync_review_inline_comments_bundle(pull_request=self.pr, reviews=[second])
        self.assertEqual(PRReviewInlineCommentBackfill.objects.count(), 1)
        backfill = PRReviewInlineCommentBackfill.objects.get(review_event=parent)
        self.assertEqual(backfill.total_count, 99)

    def test_backfill_row_skipped_when_parent_event_missing(self) -> None:
        # Without the OneToOne anchor we can't write the row; service logs a warning.
        group = self._group(
            parent_event=None,
            comments=[_comment("C_1")],
            total_count=99,
            has_next_page=True,
        )
        with self.assertLogs("syncer.services.sub.inline_comments_sync", level="WARNING") as cm:
            res = sync_review_inline_comments_bundle(pull_request=self.pr, reviews=[group])
        self.assertEqual(res.backfill_rows_upserted, 0)
        self.assertFalse(PRReviewInlineCommentBackfill.objects.exists())
        # Comment row still got written (durable link is review_node_id).
        self.assertEqual(PRReviewInlineComment.objects.count(), 1)
        self.assertTrue(any("skipped_backfill_no_parent_event" in m for m in cm.output))


class TestParseHelper(TestCase):
    def test_returns_none_for_missing_connection(self) -> None:
        self.assertIsNone(
            parse_review_inline_comments_group(
                review_node_id="REV_1",
                parent_review_event=None,
                comments_connection=None,
            )
        )

    def test_extracts_total_and_pageinfo_and_nodes(self) -> None:
        group = parse_review_inline_comments_group(
            review_node_id="REV_1",
            parent_review_event=None,
            comments_connection={
                "nodes": [
                    {"id": "A", "createdAt": "2026-01-01T00:00:00Z", "path": "x", "line": 1},
                    {"id": "B", "createdAt": "2026-01-02T00:00:00Z", "path": "y", "line": 2},
                    "not-a-dict-should-be-filtered",
                ],
                "pageInfo": {"hasNextPage": True},
                "totalCount": 17,
            },
        )
        assert group is not None
        self.assertEqual(group.total_count, 17)
        self.assertTrue(group.has_next_page)
        self.assertEqual(len(group.comment_nodes), 2)
        self.assertEqual({n["id"] for n in group.comment_nodes}, {"A", "B"})

    def test_handles_missing_pageinfo_and_total(self) -> None:
        group = parse_review_inline_comments_group(
            review_node_id="REV_1",
            parent_review_event=None,
            comments_connection={"nodes": []},
        )
        assert group is not None
        self.assertEqual(group.total_count, 0)
        self.assertFalse(group.has_next_page)
        self.assertEqual(group.comment_nodes, ())
