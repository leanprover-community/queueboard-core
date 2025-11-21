from __future__ import annotations

from unittest import mock

from django.test import TestCase
from django.utils import timezone

from core.models import Repository
from syncer.tasks.sync_tasks import sync_repo_since_task, sync_active_repos_task
from syncer.tests.factories import make_repo


class TestSyncRepoTasks(TestCase):
    def setUp(self) -> None:
        self.repo = make_repo()

    @mock.patch("syncer.tasks.sync_tasks.repo_advisory_lock")
    @mock.patch("syncer.tasks.sync_tasks.enqueue_with_parent")
    @mock.patch("syncer.tasks.sync_tasks.sync_pr_task")
    @mock.patch("syncer.tasks.sync_tasks.GitHubClient")
    def test_sync_repo_since_enqueues(self, MockClient, mock_sync_pr, mock_enqueue, mock_lock) -> None:
        # Acquire lock
        mock_lock.return_value.__enter__.return_value = True
        gh = MockClient.return_value
        gh.get_changed_pr_numbers.return_value = [1, 2, 3]
        gh.get_last_rate_limit.return_value = {"remaining": 4999, "resetAt": "2025-11-01T00:00:00Z"}

        res = sync_repo_since_task.apply(kwargs={"repo_id": self.repo.id}).get()
        self.assertFalse(res.get("skipped"))
        self.assertEqual(res.get("discovered"), 3)
        self.assertEqual(res.get("enqueued"), 3)
        # Ensure per-PR tasks were enqueued with parent headers
        self.assertEqual(mock_enqueue.call_count, 3)

    @mock.patch("syncer.tasks.sync_tasks.repo_advisory_lock")
    @mock.patch("syncer.tasks.sync_tasks.sync_pr_task")
    @mock.patch("syncer.tasks.sync_tasks.GitHubClient")
    def test_sync_repo_since_skips_when_locked(self, MockClient, mock_sync_pr, mock_lock) -> None:
        # Simulate lock not acquired
        mock_lock.return_value.__enter__.return_value = False

        res = sync_repo_since_task.apply(kwargs={"repo_id": self.repo.id}).get()
        self.assertTrue(res.get("skipped"))
        self.assertEqual(res.get("reason"), "lock_not_acquired")
        mock_sync_pr.delay.assert_not_called()
        # Client shouldn't be used if lock isn't acquired
        MockClient.assert_not_called()

    @mock.patch("syncer.tasks.sync_tasks.sync_repo_since_task")
    def test_sync_active_repos_enqueues(self, mock_repo_task) -> None:
        # One active repo exists; ensure dispatcher enqueues one task
        res = sync_active_repos_task()
        self.assertEqual(res.get("repos"), 1)
        self.assertEqual(res.get("enqueued"), 1)
        mock_repo_task.delay.assert_called_once_with(self.repo.id)

    @mock.patch("syncer.tasks.sync_tasks.repo_advisory_lock")
    @mock.patch("syncer.tasks.sync_tasks.enqueue_with_parent")
    @mock.patch("syncer.tasks.sync_tasks.sync_pr_task")
    @mock.patch("syncer.tasks.sync_tasks.GitHubClient")
    @mock.patch("syncer.tasks.sync_tasks.debounce_repo_schedule", return_value=True)
    def test_sync_repo_since_low_budget_schedules_continuation(
        self, mock_debounce, MockClient, mock_sync_pr, mock_enqueue, mock_lock
    ) -> None:
        mock_lock.return_value.__enter__.return_value = True
        gh = MockClient.return_value
        gh.get_changed_pr_numbers.return_value = [10, 11, 12]
        gh.get_last_rate_limit.return_value = {"remaining": 10, "resetAt": "2030-01-01T00:00:00Z"}

        res = sync_repo_since_task.apply(kwargs={"repo_id": self.repo.id}).get()
        self.assertTrue(res.get("low_budget"))
        # No PR tasks enqueued under low budget
        mock_enqueue.assert_called_once()
