from __future__ import annotations

from unittest import mock

from django.test import TestCase
from django.utils import timezone

from core.models import Repository
from syncer.models import PullRequest, PRTimelineEvent
from syncer.tasks.sync_tasks import sync_pr_task
from syncer.tests.factories import make_repo, make_pr


class TestSyncPrBackfillOnly(TestCase):
    def setUp(self) -> None:
        self.repo = make_repo()

    def _mk_pr(self, number: int, last_synced_at=None):
        if last_synced_at is None:
            last_synced_at = timezone.now()
        pr = make_pr(self.repo, number, last_synced_at=last_synced_at)
        pr.engagement_synced_at = last_synced_at
        pr.save(update_fields=["engagement_synced_at"])
        return pr

    @mock.patch("syncer.tasks.sync_tasks.GitHubClient")
    def test_backfill_runs_when_up_to_date(self, MockClient) -> None:
        pr = self._mk_pr(8, last_synced_at=timezone.now())
        gh = MockClient.return_value
        # Header older than last_synced_at → up-to-date path
        gh.get_pr_header.return_value = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "number": 8,
                        "updatedAt": (pr.last_synced_at - timezone.timedelta(seconds=1)).isoformat(),
                    }
                }
            }
        }
        gh.get_last_rate_limit.return_value = {"remaining": 4990, "cost": 1, "resetAt": "2030-01-01T00:00:00Z"}
        gh.get_timeline_page_back.return_value = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "timelineItems": {
                            "pageInfo": {"hasPreviousPage": False, "startCursor": "CURX"},
                            "nodes": [
                                {
                                    "__typename": "LabeledEvent",
                                    "id": "e3",
                                    "createdAt": "2023-03-01T00:00:00Z",
                                    "label": {"name": "Z"},
                                },
                            ],
                        }
                    }
                }
            }
        }

        res = sync_pr_task.apply(
            kwargs={
                "repo_id": self.repo.id,
                "number": 8,
                "backfill_timeline_pages": 1,
                "timelineK": 2,
            }
        ).get()

        self.assertFalse(res.get("skipped"))
        self.assertEqual(res.get("status"), "backfill_only")
        self.assertGreaterEqual(PRTimelineEvent.objects.filter(pull_request=pr).count(), 1)
