from __future__ import annotations

from unittest import mock

from django.test import TestCase
from django.utils import timezone
from django.conf import settings

from core.models import Repository
from syncer.models import PullRequest
from syncer.tasks.sync_tasks import sync_pr_task
from syncer.tests.factories import make_repo, make_pr


class TestSyncPrTaskSkip(TestCase):
    def setUp(self) -> None:
        self.repo = make_repo()
        self._enqueue_patcher = mock.patch("syncer.tasks.sync_tasks.enqueue_with_parent")
        self._enqueue_patcher.start()

    def tearDown(self) -> None:
        self._enqueue_patcher.stop()

    def _make_pr(self, number: int, last_synced_at=None):
        if last_synced_at is None:
            last_synced_at = timezone.now()
        pr = make_pr(self.repo, number, last_synced_at=last_synced_at)
        pr.engagement_synced_at = last_synced_at
        pr.head_ci_state = "SUCCESS"
        pr.save(update_fields=["engagement_synced_at", "head_ci_state"])
        return pr

    @mock.patch("syncer.tasks.sync_tasks.GitHubClient")
    def test_up_to_date_skip_includes_rate_events(self, MockClient) -> None:
        # Existing PR with a recent last_synced_at
        pr = self._make_pr(7, last_synced_at=timezone.now())

        gh = MockClient.return_value
        # Header updatedAt earlier than or equal to last_synced_at -> skip
        eps = int(getattr(settings, "SYNCER_LAST_SYNC_EPSILON_SECONDS", 2))
        gh.get_pr_header.return_value = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "number": 7,
                        "updatedAt": (pr.last_synced_at - timezone.timedelta(seconds=eps + 1)).isoformat(),
                    }
                }
            }
        }
        gh.get_last_rate_limit.return_value = {"remaining": 4990, "cost": 1, "resetAt": "2030-01-01T00:00:00Z"}

        res = sync_pr_task.apply(kwargs={"repo_id": self.repo.id, "number": 7}).get()
        self.assertTrue(res.get("skipped"))
        self.assertEqual(res.get("reason"), "up_to_date")
        # Ensure rate_events exists and is a list (header snapshot captured)
        self.assertIn("rate_events", res)
        self.assertIsInstance(res.get("rate_events"), list)

    @mock.patch("syncer.tasks.sync_tasks.PRSyncService.sync_pull_request")
    @mock.patch("syncer.tasks.sync_tasks.GitHubClient")
    def test_no_skip_when_state_mismatch(self, MockClient, mock_sync) -> None:
        pr = self._make_pr(9, last_synced_at=timezone.now())

        gh = MockClient.return_value
        gh.get_pr_header.return_value = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "number": 9,
                        "updatedAt": (pr.last_synced_at - timezone.timedelta(seconds=1)).isoformat(),
                        "state": "CLOSED",
                        "isDraft": False,
                    }
                }
            }
        }
        gh.get_last_rate_limit.return_value = {"remaining": 4990, "cost": 1, "resetAt": "2030-01-01T00:00:00Z"}
        mock_sync.return_value = {}

        res = sync_pr_task.apply(kwargs={"repo_id": self.repo.id, "number": 9}).get()

        self.assertFalse(res.get("skipped"))
        self.assertEqual(res.get("status"), "synced")
        mock_sync.assert_called_once()

    @mock.patch("syncer.tasks.sync_tasks.PRSyncService.sync_pull_request")
    @mock.patch("syncer.tasks.sync_tasks.GitHubClient")
    def test_no_skip_when_draft_mismatch(self, MockClient, mock_sync) -> None:
        pr = self._make_pr(11, last_synced_at=timezone.now())

        gh = MockClient.return_value
        gh.get_pr_header.return_value = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "number": 11,
                        "updatedAt": (pr.last_synced_at - timezone.timedelta(seconds=1)).isoformat(),
                        "state": "OPEN",
                        "isDraft": True,
                    }
                }
            }
        }
        gh.get_last_rate_limit.return_value = {"remaining": 4990, "cost": 1, "resetAt": "2030-01-01T00:00:00Z"}
        mock_sync.return_value = {}

        res = sync_pr_task.apply(kwargs={"repo_id": self.repo.id, "number": 11}).get()

        self.assertFalse(res.get("skipped"))
        self.assertEqual(res.get("status"), "synced")
        mock_sync.assert_called_once()
