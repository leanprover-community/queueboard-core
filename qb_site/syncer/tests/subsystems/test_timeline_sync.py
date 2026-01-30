from __future__ import annotations

from django.test import TestCase
from django.utils import timezone

from core.models.repository import Repository
from syncer.models import PullRequest, PRTimelineEvent, PRTimelineEventType
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

    def test_marks_revision_dirty_for_earlier_event(self) -> None:
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
                "__typename": "ReadyForReviewEvent",
                "id": "E4",
                "createdAt": earlier.isoformat(),
            }
        ]
        sync_timeline_events(self.pr, nodes)
        state.refresh_from_db()
        self.assertEqual(state.dirty_from_ts, earlier)

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
