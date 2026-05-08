"""End-to-end test for design doc 044 Chunk 4c.

Runs the engagement-events fixture bundle through ``PRSyncService.sync_pull_request_bundle``
and asserts that the new ``PRTimelineEvent`` rows, the ``PRReviewInlineComment``
rows, and the ``PRReviewInlineCommentBackfill`` row all appear with the
right shapes.
"""

from __future__ import annotations

import json

from django.test import TestCase

from syncer.models import PullRequest, PRTimelineEvent
from syncer.models.pr_review_inline_comment import (
    PRReviewInlineComment,
    PRReviewInlineCommentBackfill,
)
from syncer.models.pr_timeline_event import PRTimelineEventType
from syncer.services.pr_sync_service import PRSyncService
from syncer.tests.factories import make_repo
from syncer.tests.helpers import fixtures_dir


class TestEngagementEventBundleIngest(TestCase):
    def setUp(self) -> None:
        self.repo = make_repo(owner="leanprover-community", name="mathlib4")
        with open(fixtures_dir() / "pr_bundle_with_engagement_events.json") as f:
            self.bundle = json.load(f)["data"]["repository"]["pullRequest"]

    def _ingest(self) -> dict:
        return PRSyncService().sync_pull_request_bundle(self.repo, self.bundle)

    def test_creates_review_and_issue_comment_rows(self) -> None:
        result = self._ingest()
        pr = PullRequest.objects.get(repository=self.repo, number=99001)

        # 6 PRTimelineEvent rows expected from the v2 path:
        #   - 2 ReviewRequestedEvent (TL_REQ_TEAM, TL_REQ_BOT)
        #   - 1 IssueComment (TL_IC1)
        #   - 1 REVIEW_COMMENTED for TL_REV_COMMENTED
        #   - 1 REVIEW_COMMENTED for TL_REV_REPLY (one-comment thread reply review)
        #   - 1 REVIEW_APPROVED for TL_REV_APPROVED
        #   - 1 REVIEW_DISMISSED for TL_DIS_EVENT
        # = 7. (PENDING and DISMISSED PullRequestReview state nodes are
        # dropped at row creation; the dismissal is captured via the
        # ReviewDismissedEvent timeline item instead.)
        self.assertEqual(PRTimelineEvent.objects.filter(pull_request=pr).count(), 7)
        self.assertEqual(result["events_created"], 7)

    def test_review_requested_routes_team_and_bot_correctly(self) -> None:
        self._ingest()
        pr = PullRequest.objects.get(repository=self.repo, number=99001)

        team_req = PRTimelineEvent.objects.get(pull_request=pr, github_node_id="TL_REQ_TEAM")
        self.assertEqual(team_req.type, PRTimelineEventType.REVIEW_REQUESTED)
        self.assertEqual(team_req.requested_team_slug, "core-reviewers")
        self.assertIsNone(team_req.requested_reviewer_login)

        bot_req = PRTimelineEvent.objects.get(pull_request=pr, github_node_id="TL_REQ_BOT")
        self.assertEqual(bot_req.requested_reviewer_login, "dependabot")
        self.assertIsNone(bot_req.requested_team_slug)

    def test_issue_comment_row(self) -> None:
        self._ingest()
        pr = PullRequest.objects.get(repository=self.repo, number=99001)
        ev = PRTimelineEvent.objects.get(pull_request=pr, github_node_id="TL_IC1")
        self.assertEqual(ev.type, PRTimelineEventType.ISSUE_COMMENTED)
        self.assertEqual(ev.actor_login, "alice")

    def test_review_commented_persists_inline_comments(self) -> None:
        self._ingest()
        pr = PullRequest.objects.get(repository=self.repo, number=99001)
        rev_event = PRTimelineEvent.objects.get(pull_request=pr, github_node_id="TL_REV_COMMENTED")
        self.assertEqual(rev_event.type, PRTimelineEventType.REVIEW_COMMENTED)
        self.assertEqual(rev_event.inline_comment_total_count, 3)

        inline_rows = PRReviewInlineComment.objects.filter(pull_request=pr, review_node_id="TL_REV_COMMENTED").order_by(
            "github_node_id"
        )
        self.assertEqual(inline_rows.count(), 3)
        for row in inline_rows:
            # Top-level comments self-root (no replyTo).
            self.assertEqual(row.thread_root_node_id, row.github_node_id)
            self.assertIsNone(row.reply_to_node_id)
            self.assertEqual(row.parent_review_event_id, rev_event.pk)
            self.assertEqual(row.author_login, "reviewer-bob")

    def test_thread_reply_review_links_thread_root_across_reviews(self) -> None:
        self._ingest()
        pr = PullRequest.objects.get(repository=self.repo, number=99001)
        # The reply lives in its own one-comment review TL_REV_REPLY.
        reply_event = PRTimelineEvent.objects.get(pull_request=pr, github_node_id="TL_REV_REPLY")
        self.assertEqual(reply_event.type, PRTimelineEventType.REVIEW_COMMENTED)

        reply_row = PRReviewInlineComment.objects.get(github_node_id="IC_REPLY_1")
        self.assertEqual(reply_row.review_node_id, "TL_REV_REPLY")
        self.assertEqual(reply_row.reply_to_node_id, "IC_REV_1")
        # The thread root is the in-bundle target IC_REV_1, not the reply
        # itself (verifies cross-review thread resolution).
        self.assertEqual(reply_row.thread_root_node_id, "IC_REV_1")

    def test_backfill_row_created_for_review_with_more_inline_pages(self) -> None:
        self._ingest()
        pr = PullRequest.objects.get(repository=self.repo, number=99001)
        approved_event = PRTimelineEvent.objects.get(pull_request=pr, github_node_id="TL_REV_APPROVED")
        self.assertEqual(approved_event.type, PRTimelineEventType.REVIEW_APPROVED)
        self.assertEqual(approved_event.inline_comment_total_count, 25)

        backfill = PRReviewInlineCommentBackfill.objects.get(review_event=approved_event)
        self.assertEqual(backfill.review_node_id, "TL_REV_APPROVED")
        self.assertEqual(backfill.total_count, 25)
        self.assertEqual(backfill.pull_request, pr)

    def test_pending_review_dropped_no_inline_comments(self) -> None:
        self._ingest()
        pr = PullRequest.objects.get(repository=self.repo, number=99001)
        # No PRTimelineEvent row for the pending review.
        self.assertFalse(PRTimelineEvent.objects.filter(pull_request=pr, github_node_id="TL_REV_PENDING").exists())
        # Pending reviews have no comments to ingest in the fixture either.
        self.assertFalse(PRReviewInlineComment.objects.filter(pull_request=pr, review_node_id="TL_REV_PENDING").exists())

    def test_dismissed_review_inline_comments_ingested_without_parent_event(self) -> None:
        # Verified live against rust-lang/rust PR 149543: state=DISMISSED
        # PullRequestReview nodes appear in timelineItems with non-null
        # submittedAt and may have inline comments. No REVIEW_* row is
        # created for them (the dismissal is captured via the separate
        # ReviewDismissedEvent), but their inline comments are still
        # persisted with parent_review_event=NULL so they remain
        # queryable via the durable review_node_id.
        self._ingest()
        pr = PullRequest.objects.get(repository=self.repo, number=99001)
        # No REVIEW_* row for the dismissed PullRequestReview itself.
        self.assertFalse(PRTimelineEvent.objects.filter(pull_request=pr, github_node_id="TL_REV_DISMISSED").exists())
        # But REVIEW_DISMISSED row from the ReviewDismissedEvent exists,
        # with the dismissed review denormalized into extra.
        dis = PRTimelineEvent.objects.get(pull_request=pr, github_node_id="TL_DIS_EVENT")
        self.assertEqual(dis.type, PRTimelineEventType.REVIEW_DISMISSED)
        self.assertEqual(dis.actor_login, "test-author")
        self.assertEqual(dis.extra["dismissed_review_node_id"], "TL_REV_DISMISSED")
        self.assertEqual(dis.extra["previous_review_state"], "CHANGES_REQUESTED")
        # The inline comment under the dismissed review IS captured.
        ic = PRReviewInlineComment.objects.get(github_node_id="IC_DISMISSED_1")
        self.assertEqual(ic.review_node_id, "TL_REV_DISMISSED")
        self.assertEqual(ic.author_login, "reviewer-erin")
        self.assertIsNone(ic.parent_review_event_id)

    def test_idempotent_under_re_ingest(self) -> None:
        self._ingest()
        before_events = PRTimelineEvent.objects.count()
        before_inline = PRReviewInlineComment.objects.count()
        before_backfill = PRReviewInlineCommentBackfill.objects.count()

        # Re-ingest the same bundle: no new rows should appear.
        self._ingest()
        self.assertEqual(PRTimelineEvent.objects.count(), before_events)
        self.assertEqual(PRReviewInlineComment.objects.count(), before_inline)
        self.assertEqual(PRReviewInlineCommentBackfill.objects.count(), before_backfill)

    def test_result_dict_surfaces_inline_counts(self) -> None:
        result = self._ingest()
        # 6 inline comments captured: 3 (TL_REV_COMMENTED) + 1 reply (TL_REV_REPLY)
        # + 1 first-page node from TL_REV_APPROVED + 1 from TL_REV_DISMISSED.
        self.assertEqual(result["inline_comments_created"], 6)
        # Only TL_REV_APPROVED had hasNextPage=true; TL_REV_DISMISSED has no
        # parent event so its backfill marker is skipped (hasNextPage=false in
        # the fixture anyway).
        self.assertEqual(result["inline_backfill_rows_upserted"], 1)
