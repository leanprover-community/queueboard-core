from __future__ import annotations

from django.test import TestCase
from django.utils import timezone

from syncer.models import PullRequest
from syncer.services.sub.timeline_sync import sync_timeline_events
from syncer.tests.factories import make_repo, make_pr


class TestCommitBackfillForcePushReset(TestCase):
    def setUp(self) -> None:
        repo = make_repo()
        self.pr: PullRequest = make_pr(repo, 21, last_synced_at=timezone.now())
        self.pr.commits_backfill_cursor = "CURX"
        self.pr.commits_backfill_done = True
        self.pr.commits_earliest_synced_at = timezone.now()
        self.pr.save(
            update_fields=[
                "commits_backfill_cursor",
                "commits_backfill_done",
                "commits_earliest_synced_at",
            ]
        )

    def test_head_force_push_event_resets_commit_backfill_state(self) -> None:
        events = [
            {
                "__typename": "HeadRefForcePushedEvent",
                "id": "E1",
                "createdAt": "2024-02-01T00:00:00Z",
                "beforeCommit": {"oid": "OLD"},
                "afterCommit": {"oid": "NEW"},
            }
        ]

        sync_timeline_events(self.pr, events)

        pr = PullRequest.objects.get(id=self.pr.id)
        self.assertFalse(pr.commits_backfill_done)
        self.assertIsNone(pr.commits_backfill_cursor)
        self.assertIsNone(pr.commits_earliest_synced_at)
