from __future__ import annotations

from django.test import TestCase
from django.utils import timezone

from core.models.repository import Repository
from syncer.models import PullRequest, PRTimelineEvent
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
