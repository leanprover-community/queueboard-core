from __future__ import annotations

from django.test import TestCase
from django.utils import timezone

from core.models.repository import Repository
from syncer.models import PullRequest, PRTimelineEvent
from syncer.services.sub.timeline_sync import sync_timeline_events


class TestTimelineSync(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="o", name="r", default_branch="master", is_active=True)
        self.pr = PullRequest.objects.create(
            repository=self.repo,
            number=1,
            state="open",
            is_draft=False,
            gh_created_at=timezone.now(),
            gh_updated_at=timezone.now(),
            base_ref_name="master",
            head_ref_name="b",
            head_repo_owner_login="o",
            head_repo_name="fork",
            title="t",
            body="",
            additions=0,
            deletions=0,
            changed_files_count=0,
        )

    def test_insert_and_dedupe(self) -> None:
        nodes = [
            {"__typename": "LabeledEvent", "id": "E1", "createdAt": "2025-10-20T00:00:00Z", "label": {"name": "easy"}},
            {"__typename": "LabeledEvent", "id": "E1", "createdAt": "2025-10-20T00:00:00Z", "label": {"name": "easy"}},
            {"__typename": "ClosedEvent", "id": "E2", "createdAt": "2025-10-20T01:00:00Z"},
            {"__typename": "SomeOtherEvent", "id": "E3", "createdAt": "2025-10-20T02:00:00Z"}
        ]
        res = sync_timeline_events(self.pr, nodes)
        self.assertEqual(res.created, 2)
        self.assertEqual(PRTimelineEvent.objects.filter(pull_request=self.pr).count(), 2)
