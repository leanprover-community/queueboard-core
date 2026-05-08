from __future__ import annotations

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from syncer.models import PRTimelineEvent, PRTimelineEventType
from syncer.services.sub.timeline_sync import sync_timeline_events
from syncer.tests.factories import make_repo, make_pr
from analyzer.models import PRRevisionBuildState


class TestTimelineSync(TestCase):
    def setUp(self) -> None:
        self.repo = make_repo()
        self.pr = make_pr(self.repo, 1)

    def test_insert_and_dedupe(self) -> None:
        nodes = [
            {"__typename": "LabeledEvent", "id": "E1", "createdAt": "2025-10-20T00:00:00Z", "label": {"name": "easy"}},
            {"__typename": "LabeledEvent", "id": "E1", "createdAt": "2025-10-20T00:00:00Z", "label": {"name": "easy"}},
            {"__typename": "ClosedEvent", "id": "E2", "createdAt": "2025-10-20T01:00:00Z"},
            {"__typename": "SomeOtherEvent", "id": "E3", "createdAt": "2025-10-20T02:00:00Z"},
        ]
        res = sync_timeline_events(self.pr, nodes)
        self.assertEqual(res.created, 2)
        self.assertEqual(PRTimelineEvent.objects.filter(pull_request=self.pr).count(), 2)

    def test_force_push_event_persists_shas(self) -> None:
        nodes = [
            {
                "__typename": "HeadRefForcePushedEvent",
                "id": "FP1",
                "createdAt": "2025-10-21T00:00:00Z",
                "beforeCommit": {"oid": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
                "afterCommit": {"oid": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},
            }
        ]
        res = sync_timeline_events(self.pr, nodes)
        self.assertEqual(res.created, 1)
        ev = PRTimelineEvent.objects.get(pull_request=self.pr)
        self.assertEqual(ev.type, "HEAD_FORCE_PUSHED")
        self.assertEqual(ev.before_sha, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        self.assertEqual(ev.after_sha, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")

    def test_marks_revision_dirty_for_earlier_force_push_event(self) -> None:
        # Seed build state as if revisions were built through a later timestamp.
        built_through = timezone.now()
        state = PRRevisionBuildState.objects.create(
            pull_request=self.pr,
            built_through_ts=built_through,
            dirty_from_ts=None,
        )
        earlier = (built_through - timezone.timedelta(hours=2)).replace(microsecond=0)
        nodes = [
            {
                "__typename": "HeadRefForcePushedEvent",
                "id": "E4",
                "createdAt": earlier.isoformat(),
                "beforeCommit": {"oid": "a" * 40},
                "afterCommit": {"oid": "b" * 40},
            }
        ]
        sync_timeline_events(self.pr, nodes)
        state.refresh_from_db()
        self.assertEqual(state.dirty_from_ts, earlier)

    def test_does_not_mark_revision_dirty_for_non_revision_event(self) -> None:
        built_through = timezone.now()
        state = PRRevisionBuildState.objects.create(
            pull_request=self.pr,
            built_through_ts=built_through,
            dirty_from_ts=None,
        )
        earlier = (built_through - timezone.timedelta(hours=2)).replace(microsecond=0)
        nodes = [
            {
                "__typename": "ReadyForReviewEvent",
                "id": "E_NON_REV",
                "createdAt": earlier.isoformat(),
            }
        ]
        sync_timeline_events(self.pr, nodes)
        state.refresh_from_db()
        self.assertIsNone(state.dirty_from_ts)

    def test_non_revision_event_does_not_move_existing_dirty_marker(self) -> None:
        built_through = timezone.now().replace(microsecond=0)
        existing_dirty = built_through - timezone.timedelta(hours=1)
        state = PRRevisionBuildState.objects.create(
            pull_request=self.pr,
            built_through_ts=built_through,
            dirty_from_ts=existing_dirty,
        )
        older_non_revision = built_through - timezone.timedelta(hours=2)
        nodes = [
            {
                "__typename": "ReadyForReviewEvent",
                "id": "E_NON_REV_OLDER",
                "createdAt": older_non_revision.isoformat(),
            }
        ]
        sync_timeline_events(self.pr, nodes)
        state.refresh_from_db()
        self.assertEqual(state.dirty_from_ts, existing_dirty)

    def test_updates_missing_actor_and_assignee(self) -> None:
        existing = PRTimelineEvent.objects.create(
            pull_request=self.pr,
            github_node_id="A1",
            type=PRTimelineEventType.ASSIGNED,
            occurred_at=timezone.now(),
        )
        nodes = [
            {
                "__typename": "AssignedEvent",
                "id": "A1",
                "createdAt": "2025-10-22T00:00:00Z",
                "actor": {"login": "bot"},
                "assignee": {"login": "alice"},
            }
        ]
        res = sync_timeline_events(self.pr, nodes)
        self.assertEqual(res.created, 0)
        self.assertEqual(res.updated, 1)
        existing.refresh_from_db()
        self.assertEqual(existing.actor_login, "bot")
        self.assertEqual(existing.assignee_login, "alice")

    def test_updates_missing_label_and_actor(self) -> None:
        existing = PRTimelineEvent.objects.create(
            pull_request=self.pr,
            github_node_id="L1",
            type=PRTimelineEventType.LABELED,
            occurred_at=timezone.now(),
        )
        nodes = [
            {
                "__typename": "LabeledEvent",
                "id": "L1",
                "createdAt": "2025-10-22T00:10:00Z",
                "actor": {"login": "carol"},
                "label": {"name": "easy"},
            }
        ]
        res = sync_timeline_events(self.pr, nodes)
        self.assertEqual(res.created, 0)
        self.assertEqual(res.updated, 1)
        existing.refresh_from_db()
        self.assertEqual(existing.actor_login, "carol")
        self.assertEqual(existing.label_name, "easy")

    def test_does_not_overwrite_existing_fields(self) -> None:
        existing = PRTimelineEvent.objects.create(
            pull_request=self.pr,
            github_node_id="U1",
            type=PRTimelineEventType.UNASSIGNED,
            occurred_at=timezone.now(),
            actor_login="alice",
            assignee_login="bob",
        )
        nodes = [
            {
                "__typename": "UnassignedEvent",
                "id": "U1",
                "createdAt": "2025-10-22T00:20:00Z",
                "actor": {"login": "carol"},
                "assignee": {"login": "dave"},
            }
        ]
        res = sync_timeline_events(self.pr, nodes)
        self.assertEqual(res.created, 0)
        self.assertEqual(res.updated, 0)
        existing.refresh_from_db()
        self.assertEqual(existing.actor_login, "alice")
        self.assertEqual(existing.assignee_login, "bob")

    def test_fills_missing_fields_across_multiple_syncs(self) -> None:
        nodes_initial = [
            {
                "__typename": "AssignedEvent",
                "id": "A2",
                "createdAt": "2025-10-22T01:00:00Z",
                "assignee": {"login": "alice"},
            },
            {
                "__typename": "LabeledEvent",
                "id": "L2",
                "createdAt": "2025-10-22T01:05:00Z",
                "label": {"name": "easy"},
            },
        ]
        res1 = sync_timeline_events(self.pr, nodes_initial)
        self.assertEqual(res1.created, 2)
        self.assertEqual(res1.updated, 0)

        nodes_fill = [
            {
                "__typename": "AssignedEvent",
                "id": "A2",
                "createdAt": "2025-10-22T01:00:00Z",
                "actor": {"login": "bot"},
                "assignee": {"login": "alice"},
            },
            {
                "__typename": "LabeledEvent",
                "id": "L2",
                "createdAt": "2025-10-22T01:05:00Z",
                "actor": {"login": "carol"},
                "label": {"name": "easy"},
            },
        ]
        res2 = sync_timeline_events(self.pr, nodes_fill)
        self.assertEqual(res2.created, 0)
        self.assertEqual(res2.updated, 2)

        nodes_stale = [
            {
                "__typename": "AssignedEvent",
                "id": "A2",
                "createdAt": "2025-10-22T01:00:00Z",
                "assignee": {"login": "alice"},
            }
        ]
        res3 = sync_timeline_events(self.pr, nodes_stale)
        self.assertEqual(res3.created, 0)
        self.assertEqual(res3.updated, 0)

        assigned = PRTimelineEvent.objects.get(pull_request=self.pr, github_node_id="A2")
        labeled = PRTimelineEvent.objects.get(pull_request=self.pr, github_node_id="L2")
        self.assertEqual(assigned.actor_login, "bot")
        self.assertEqual(assigned.assignee_login, "alice")
        self.assertEqual(labeled.actor_login, "carol")
        self.assertEqual(labeled.label_name, "easy")

    def test_updates_existing_and_creates_new_in_same_sync(self) -> None:
        existing = PRTimelineEvent.objects.create(
            pull_request=self.pr,
            github_node_id="A3",
            type=PRTimelineEventType.ASSIGNED,
            occurred_at=timezone.now(),
        )
        nodes = [
            {
                "__typename": "AssignedEvent",
                "id": "A3",
                "createdAt": "2025-10-22T02:00:00Z",
                "actor": {"login": "bot"},
                "assignee": {"login": "alice"},
            },
            {
                "__typename": "UnassignedEvent",
                "id": "U3",
                "createdAt": "2025-10-22T02:05:00Z",
                "actor": {"login": "alice"},
                "assignee": {"login": "alice"},
            },
        ]
        res = sync_timeline_events(self.pr, nodes)
        self.assertEqual(res.created, 1)
        self.assertEqual(res.updated, 1)
        self.assertEqual(PRTimelineEvent.objects.filter(pull_request=self.pr).count(), 2)
        existing.refresh_from_db()
        self.assertEqual(existing.actor_login, "bot")
        self.assertEqual(existing.assignee_login, "alice")


class TestTimelineSyncReviewAndCommentEvents(TestCase):
    """Coverage for the v2 event types added by design doc 044 (Chunk 4b)."""

    def setUp(self) -> None:
        self.repo = make_repo()
        self.pr = make_pr(self.repo, 1)

    def test_issue_comment_creates_row(self) -> None:
        nodes = [
            {
                "__typename": "IssueComment",
                "id": "IC1",
                "createdAt": "2026-05-01T12:00:00Z",
                "author": {"__typename": "User", "login": "alice"},
            }
        ]
        res = sync_timeline_events(self.pr, nodes)
        self.assertEqual(res.created, 1)
        ev = PRTimelineEvent.objects.get(pull_request=self.pr, github_node_id="IC1")
        self.assertEqual(ev.type, PRTimelineEventType.ISSUE_COMMENTED)
        self.assertEqual(ev.actor_login, "alice")
        self.assertIsNone(ev.inline_comment_total_count)

    def test_issue_comment_bot_author(self) -> None:
        nodes = [
            {
                "__typename": "IssueComment",
                "id": "IC2",
                "createdAt": "2026-05-01T12:00:00Z",
                "author": {"__typename": "Bot", "login": "dependabot"},
            }
        ]
        sync_timeline_events(self.pr, nodes)
        ev = PRTimelineEvent.objects.get(pull_request=self.pr, github_node_id="IC2")
        self.assertEqual(ev.actor_login, "dependabot")

    def test_issue_comment_null_author_stored_as_empty_string(self) -> None:
        nodes = [
            {
                "__typename": "IssueComment",
                "id": "IC3",
                "createdAt": "2026-05-01T12:00:00Z",
                "author": None,
            }
        ]
        sync_timeline_events(self.pr, nodes)
        ev = PRTimelineEvent.objects.get(pull_request=self.pr, github_node_id="IC3")
        self.assertEqual(ev.actor_login, "")

    def test_review_state_routing_for_each_terminal_state(self) -> None:
        nodes = [
            {
                "__typename": "PullRequestReview",
                "id": "RV-A",
                "state": "APPROVED",
                "submittedAt": "2026-05-01T12:00:00Z",
                "author": {"__typename": "User", "login": "alice"},
                "comments": {"totalCount": 0, "pageInfo": {"hasNextPage": False}, "nodes": []},
            },
            {
                "__typename": "PullRequestReview",
                "id": "RV-CR",
                "state": "CHANGES_REQUESTED",
                "submittedAt": "2026-05-01T12:05:00Z",
                "author": {"__typename": "User", "login": "bob"},
                "comments": {"totalCount": 2, "pageInfo": {"hasNextPage": False}, "nodes": []},
            },
            {
                "__typename": "PullRequestReview",
                "id": "RV-C",
                "state": "COMMENTED",
                "submittedAt": "2026-05-01T12:10:00Z",
                "author": {"__typename": "User", "login": "carol"},
                "comments": {"totalCount": 1, "pageInfo": {"hasNextPage": False}, "nodes": []},
            },
        ]
        res = sync_timeline_events(self.pr, nodes)
        self.assertEqual(res.created, 3)

        approved = PRTimelineEvent.objects.get(pull_request=self.pr, github_node_id="RV-A")
        self.assertEqual(approved.type, PRTimelineEventType.REVIEW_APPROVED)
        self.assertEqual(approved.actor_login, "alice")
        self.assertEqual(approved.inline_comment_total_count, 0)

        cr = PRTimelineEvent.objects.get(pull_request=self.pr, github_node_id="RV-CR")
        self.assertEqual(cr.type, PRTimelineEventType.REVIEW_CHANGES_REQUESTED)
        self.assertEqual(cr.inline_comment_total_count, 2)

        commented = PRTimelineEvent.objects.get(pull_request=self.pr, github_node_id="RV-C")
        self.assertEqual(commented.type, PRTimelineEventType.REVIEW_COMMENTED)
        self.assertEqual(commented.inline_comment_total_count, 1)

    def test_pending_review_is_dropped(self) -> None:
        nodes = [
            {
                "__typename": "PullRequestReview",
                "id": "RV-P",
                "state": "PENDING",
                "submittedAt": None,
                "author": {"__typename": "User", "login": "alice"},
                "comments": {"totalCount": 0, "pageInfo": {"hasNextPage": False}, "nodes": []},
            }
        ]
        res = sync_timeline_events(self.pr, nodes)
        self.assertEqual(res.created, 0)
        self.assertFalse(PRTimelineEvent.objects.filter(pull_request=self.pr).exists())

    def test_dismissed_review_state_on_pull_request_review_is_dropped(self) -> None:
        # State=DISMISSED on a PullRequestReview node is captured via the
        # separate ReviewDismissedEvent timeline item, not as REVIEW_*.
        nodes = [
            {
                "__typename": "PullRequestReview",
                "id": "RV-D",
                "state": "DISMISSED",
                "submittedAt": "2026-05-01T12:00:00Z",
                "author": {"__typename": "User", "login": "alice"},
                "comments": {"totalCount": 0, "pageInfo": {"hasNextPage": False}, "nodes": []},
            }
        ]
        res = sync_timeline_events(self.pr, nodes)
        self.assertEqual(res.created, 0)

    def test_inline_comment_total_count_refreshes_to_higher_value(self) -> None:
        # Initial sync sees totalCount=2; a later sync sees 5 (the long tail
        # was added between syncs). The column should reflect the new value.
        nodes_v1 = [
            {
                "__typename": "PullRequestReview",
                "id": "RV-T",
                "state": "COMMENTED",
                "submittedAt": "2026-05-01T12:00:00Z",
                "author": {"__typename": "User", "login": "alice"},
                "comments": {"totalCount": 2, "pageInfo": {"hasNextPage": False}, "nodes": []},
            }
        ]
        sync_timeline_events(self.pr, nodes_v1)
        nodes_v2 = [
            {
                **nodes_v1[0],
                "comments": {"totalCount": 5, "pageInfo": {"hasNextPage": True}, "nodes": []},
            }
        ]
        res = sync_timeline_events(self.pr, nodes_v2)
        ev = PRTimelineEvent.objects.get(pull_request=self.pr, github_node_id="RV-T")
        self.assertEqual(ev.inline_comment_total_count, 5)
        self.assertEqual(res.updated, 1)

    def test_review_dismissed_event_denormalizes_review_into_extra(self) -> None:
        nodes = [
            {
                "__typename": "ReviewDismissedEvent",
                "id": "DIS1",
                "createdAt": "2026-05-01T12:00:00Z",
                "previousReviewState": "APPROVED",
                "actor": {"__typename": "User", "login": "admin"},
                "review": {
                    "id": "OLDREV",
                    "submittedAt": "2026-04-29T08:00:00Z",
                    "author": {"__typename": "User", "login": "reviewer"},
                },
            }
        ]
        sync_timeline_events(self.pr, nodes)
        ev = PRTimelineEvent.objects.get(pull_request=self.pr, github_node_id="DIS1")
        self.assertEqual(ev.type, PRTimelineEventType.REVIEW_DISMISSED)
        # Actor is the dismisser, NOT the dismissed review's author.
        self.assertEqual(ev.actor_login, "admin")
        self.assertEqual(ev.extra["dismissed_review_node_id"], "OLDREV")
        self.assertEqual(ev.extra["dismissed_review_author"], "reviewer")
        self.assertEqual(ev.extra["dismissed_review_submitted_at"], "2026-04-29T08:00:00Z")
        self.assertEqual(ev.extra["previous_review_state"], "APPROVED")

    def test_review_dismissed_event_handles_null_review(self) -> None:
        # Phase-0 confirmed ReviewDismissedEvent.review is nullable. We must
        # store the row with previous_review_state but omit dismissed_review_*.
        nodes = [
            {
                "__typename": "ReviewDismissedEvent",
                "id": "DIS2",
                "createdAt": "2026-05-01T12:00:00Z",
                "previousReviewState": "CHANGES_REQUESTED",
                "actor": {"__typename": "User", "login": "admin"},
                "review": None,
            }
        ]
        sync_timeline_events(self.pr, nodes)
        ev = PRTimelineEvent.objects.get(pull_request=self.pr, github_node_id="DIS2")
        self.assertEqual(ev.actor_login, "admin")
        self.assertEqual(ev.extra, {"previous_review_state": "CHANGES_REQUESTED"})

    def test_review_requested_user(self) -> None:
        nodes = [
            {
                "__typename": "ReviewRequestedEvent",
                "id": "REQ1",
                "createdAt": "2026-05-01T12:00:00Z",
                "actor": {"__typename": "User", "login": "asker"},
                "requestedReviewer": {"__typename": "User", "login": "alice"},
            }
        ]
        sync_timeline_events(self.pr, nodes)
        ev = PRTimelineEvent.objects.get(pull_request=self.pr, github_node_id="REQ1")
        self.assertEqual(ev.type, PRTimelineEventType.REVIEW_REQUESTED)
        self.assertEqual(ev.actor_login, "asker")
        self.assertEqual(ev.requested_reviewer_login, "alice")
        self.assertIsNone(ev.requested_team_slug)

    def test_review_requested_team(self) -> None:
        nodes = [
            {
                "__typename": "ReviewRequestedEvent",
                "id": "REQ2",
                "createdAt": "2026-05-01T12:00:00Z",
                "actor": {"__typename": "User", "login": "asker"},
                "requestedReviewer": {"__typename": "Team", "slug": "core-reviewers"},
            }
        ]
        sync_timeline_events(self.pr, nodes)
        ev = PRTimelineEvent.objects.get(pull_request=self.pr, github_node_id="REQ2")
        self.assertEqual(ev.requested_team_slug, "core-reviewers")
        self.assertIsNone(ev.requested_reviewer_login)

    def test_review_requested_bot_routes_to_login_column(self) -> None:
        # Phase 0 confirmed Bot is part of the RequestedReviewer union; route
        # it to requested_reviewer_login since Bot has a login.
        nodes = [
            {
                "__typename": "ReviewRequestedEvent",
                "id": "REQ3",
                "createdAt": "2026-05-01T12:00:00Z",
                "actor": {"__typename": "User", "login": "asker"},
                "requestedReviewer": {"__typename": "Bot", "login": "dependabot"},
            }
        ]
        sync_timeline_events(self.pr, nodes)
        ev = PRTimelineEvent.objects.get(pull_request=self.pr, github_node_id="REQ3")
        self.assertEqual(ev.requested_reviewer_login, "dependabot")
        self.assertIsNone(ev.requested_team_slug)

    def test_review_requested_mannequin_routes_to_login_column(self) -> None:
        nodes = [
            {
                "__typename": "ReviewRequestedEvent",
                "id": "REQ4",
                "createdAt": "2026-05-01T12:00:00Z",
                "actor": {"__typename": "User", "login": "asker"},
                "requestedReviewer": {"__typename": "Mannequin", "login": "ghost-user"},
            }
        ]
        sync_timeline_events(self.pr, nodes)
        ev = PRTimelineEvent.objects.get(pull_request=self.pr, github_node_id="REQ4")
        self.assertEqual(ev.requested_reviewer_login, "ghost-user")

    def test_review_request_removed_uses_same_routing(self) -> None:
        nodes = [
            {
                "__typename": "ReviewRequestRemovedEvent",
                "id": "REM1",
                "createdAt": "2026-05-01T12:00:00Z",
                "actor": {"__typename": "User", "login": "asker"},
                "requestedReviewer": {"__typename": "Team", "slug": "core-reviewers"},
            }
        ]
        sync_timeline_events(self.pr, nodes)
        ev = PRTimelineEvent.objects.get(pull_request=self.pr, github_node_id="REM1")
        self.assertEqual(ev.type, PRTimelineEventType.REVIEW_REQUEST_REMOVED)
        self.assertEqual(ev.requested_team_slug, "core-reviewers")

    def test_review_requested_null_actor_is_empty_string(self) -> None:
        nodes = [
            {
                "__typename": "ReviewRequestedEvent",
                "id": "REQ5",
                "createdAt": "2026-05-01T12:00:00Z",
                "actor": None,
                "requestedReviewer": {"__typename": "User", "login": "alice"},
            }
        ]
        sync_timeline_events(self.pr, nodes)
        ev = PRTimelineEvent.objects.get(pull_request=self.pr, github_node_id="REQ5")
        self.assertEqual(ev.actor_login, "")
        self.assertEqual(ev.requested_reviewer_login, "alice")


class TestTimelineEventCheckConstraints(TestCase):
    """Negative cases for the v2 CHECK constraints on PRTimelineEvent.

    Happy paths are covered by TestTimelineSyncReviewAndCommentEvents above;
    these tests just guard the database-level invariants by attempting writes
    that bypass the syncer's normalizer.
    """

    def setUp(self) -> None:
        self.repo = make_repo()
        self.pr = make_pr(self.repo, 1)
        self.now = timezone.now()

    def _create(self, **fields):
        return PRTimelineEvent.objects.create(
            pull_request=self.pr,
            occurred_at=self.now,
            **fields,
        )

    def test_requested_reviewer_login_rejected_on_non_request_type(self) -> None:
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._create(
                    type=PRTimelineEventType.ISSUE_COMMENTED,
                    requested_reviewer_login="alice",
                )

    def test_requested_team_slug_rejected_on_non_request_type(self) -> None:
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._create(
                    type=PRTimelineEventType.REVIEW_APPROVED,
                    requested_team_slug="reviewers",
                )

    def test_both_reviewer_columns_set_is_rejected(self) -> None:
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._create(
                    type=PRTimelineEventType.REVIEW_REQUESTED,
                    requested_reviewer_login="alice",
                    requested_team_slug="reviewers",
                )

    def test_inline_total_rejected_on_non_review_submission_type(self) -> None:
        for bad_type in (
            PRTimelineEventType.REVIEW_DISMISSED,
            PRTimelineEventType.REVIEW_REQUESTED,
            PRTimelineEventType.ISSUE_COMMENTED,
        ):
            with self.subTest(type=bad_type):
                with self.assertRaises(IntegrityError):
                    with transaction.atomic():
                        self._create(type=bad_type, inline_comment_total_count=3)

    def test_review_request_event_with_single_column_is_allowed(self) -> None:
        # Sanity: the constraint must not reject the documented happy path.
        ev_user = self._create(
            type=PRTimelineEventType.REVIEW_REQUESTED,
            requested_reviewer_login="alice",
        )
        ev_team = self._create(
            type=PRTimelineEventType.REVIEW_REQUEST_REMOVED,
            requested_team_slug="reviewers",
        )
        self.assertIsNotNone(ev_user.pk)
        self.assertIsNotNone(ev_team.pk)

    def test_review_submission_with_inline_total_is_allowed(self) -> None:
        for ok_type in (
            PRTimelineEventType.REVIEW_APPROVED,
            PRTimelineEventType.REVIEW_CHANGES_REQUESTED,
            PRTimelineEventType.REVIEW_COMMENTED,
        ):
            with self.subTest(type=ok_type):
                ev = self._create(type=ok_type, inline_comment_total_count=5)
                self.assertEqual(ev.inline_comment_total_count, 5)

    def test_request_event_without_either_column_is_allowed(self) -> None:
        # GitHub may return null requestedReviewer for deleted/anonymized
        # accounts; design intentionally does not enforce the inverse.
        ev = self._create(type=PRTimelineEventType.REVIEW_REQUESTED)
        self.assertIsNone(ev.requested_reviewer_login)
        self.assertIsNone(ev.requested_team_slug)
