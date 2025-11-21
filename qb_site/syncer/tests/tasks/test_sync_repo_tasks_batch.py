from __future__ import annotations

from unittest import mock

from django.test import TestCase, override_settings

from core.models import Repository
from syncer.tasks.sync_tasks import sync_repo_since_task
from syncer.tests.factories import make_repo


class TestSyncRepoTasksBatching(TestCase):
    def setUp(self) -> None:
        self.repo = make_repo()

    @override_settings(SYNCER_RATE_REMAINING_MIN=200, SYNCER_REPO_ENQUEUE_BATCH_MAX=30, SYNCER_EST_COST_PER_PR=150)
    @mock.patch("syncer.tasks.sync_tasks.repo_advisory_lock")
    @mock.patch("syncer.tasks.sync_tasks.sync_pr_task")
    @mock.patch("syncer.tasks.sync_tasks.enqueue_with_parent")
    @mock.patch("syncer.tasks.sync_tasks.GitHubClient")
    def test_batch_sizing_enqueues_subset(self, MockClient, mock_enqueue, mock_sync_pr, mock_lock) -> None:
        mock_lock.return_value.__enter__.return_value = True
        gh = MockClient.return_value
        # 10 candidates discovered
        gh.get_changed_pr_numbers.return_value = list(range(1, 11))
        # remaining 1000, threshold 200 -> allowed=800 -> dynamic_cap=5
        gh.get_last_rate_limit.return_value = {"remaining": 1000, "resetAt": "2030-01-01T00:00:00Z"}

        res = sync_repo_since_task.apply(kwargs={"repo_id": self.repo.id}).get()
        # Should enqueue only 5 now
        self.assertEqual(mock_enqueue.call_count, 5)
        self.assertEqual(res.get("enqueued"), 5)
        self.assertFalse(res.get("low_budget"))
