from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from syncer.models import RepoDiscoveryState
from syncer.tasks.sync_tasks import sync_repo_since_task
from syncer.tests.factories import make_repo


class TestSyncRepoDiscoveryStateMachine(TestCase):
    def setUp(self) -> None:
        self.repo = make_repo(owner="leanprover-community", name="mathlib4")

    @mock.patch("syncer.tasks.sync_tasks.repo_advisory_lock")
    @mock.patch("syncer.tasks.sync_tasks.enqueue_with_parent")
    @mock.patch("syncer.tasks.sync_tasks.sync_pr_task")
    @mock.patch("syncer.tasks.sync_tasks.GitHubClient")
    def test_continuation_completes_and_advances_watermark(self, MockClient, mock_sync_pr, mock_enqueue, mock_lock) -> None:
        mock_lock.return_value.__enter__.return_value = True
        gh = MockClient.return_value
        gh.get_last_rate_limit.return_value = {"remaining": 9999, "resetAt": "2030-01-01T00:00:00Z", "cost": 1}

        # First run: incomplete scan, leaves continuation cursor.
        gh.discover_changed_pr_numbers.return_value = mock.Mock(
            numbers=[101, 102],
            reached_cutoff=False,
            next_cursor="CUR-1",
            hit_limit=True,
        )
        res1 = sync_repo_since_task.apply(kwargs={"repo_id": self.repo.id}).get()
        self.assertFalse(res1["scan_complete"])
        state = RepoDiscoveryState.objects.get(repository=self.repo)
        self.assertIsNone(state.last_successful_cutoff_at)
        self.assertEqual(state.continuation_cursor, "CUR-1")

        # Second run: continuation reaches cutoff and finalizes watermark.
        gh.discover_changed_pr_numbers.return_value = mock.Mock(
            numbers=[103],
            reached_cutoff=True,
            next_cursor=None,
            hit_limit=False,
        )
        res2 = sync_repo_since_task.apply(kwargs={"repo_id": self.repo.id}).get()
        self.assertTrue(res2["scan_complete"])
        self.assertEqual(res2["mode"], "continuation")
        state.refresh_from_db()
        self.assertIsNotNone(state.last_successful_cutoff_at)
        self.assertIsNotNone(state.last_successful_at)
        self.assertIsNone(state.continuation_cutoff_at)
        self.assertIsNone(state.continuation_cursor)
        self.assertIsNone(state.continuation_started_at)

    @mock.patch("syncer.tasks.sync_tasks.repo_advisory_lock")
    @mock.patch("syncer.tasks.sync_tasks.enqueue_with_parent")
    @mock.patch("syncer.tasks.sync_tasks.sync_pr_task")
    @mock.patch("syncer.tasks.sync_tasks.GitHubClient")
    def test_partial_runs_do_not_advance_watermark(self, MockClient, mock_sync_pr, mock_enqueue, mock_lock) -> None:
        mock_lock.return_value.__enter__.return_value = True
        gh = MockClient.return_value
        gh.get_last_rate_limit.return_value = {"remaining": 9999, "resetAt": "2030-01-01T00:00:00Z", "cost": 1}
        gh.discover_changed_pr_numbers.return_value = mock.Mock(
            numbers=[1],
            reached_cutoff=False,
            next_cursor="CUR-1",
            hit_limit=True,
        )

        res = sync_repo_since_task.apply(kwargs={"repo_id": self.repo.id}).get()
        self.assertFalse(res["scan_complete"])
        state = RepoDiscoveryState.objects.get(repository=self.repo)
        self.assertIsNone(state.last_successful_cutoff_at)
        self.assertEqual(state.continuation_cursor, "CUR-1")

    @override_settings(SYNCER_DISCOVERY_LOOKBACK_MINUTES=10, SYNCER_DISCOVERY_OVERLAP_SECONDS=300)
    @mock.patch("syncer.tasks.sync_tasks.repo_advisory_lock")
    @mock.patch("syncer.tasks.sync_tasks.enqueue_with_parent")
    @mock.patch("syncer.tasks.sync_tasks.sync_pr_task")
    @mock.patch("syncer.tasks.sync_tasks.GitHubClient")
    def test_overlap_cutoff_uses_watermark_minus_overlap(self, MockClient, mock_sync_pr, mock_enqueue, mock_lock) -> None:
        mock_lock.return_value.__enter__.return_value = True
        watermark = timezone.now() - timedelta(minutes=20)
        RepoDiscoveryState.objects.create(repository=self.repo, last_successful_cutoff_at=watermark)
        gh = MockClient.return_value
        gh.get_last_rate_limit.return_value = {"remaining": 9999, "resetAt": "2030-01-01T00:00:00Z", "cost": 1}
        gh.discover_changed_pr_numbers.return_value = mock.Mock(
            numbers=[],
            reached_cutoff=True,
            next_cursor=None,
            hit_limit=False,
        )

        sync_repo_since_task.apply(kwargs={"repo_id": self.repo.id}).get()
        args = gh.discover_changed_pr_numbers.call_args
        self.assertIsNotNone(args)
        assert args is not None
        expected = (watermark - timedelta(seconds=300)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(args.kwargs["since_iso"], expected)
        state = RepoDiscoveryState.objects.get(repository=self.repo)
        self.assertGreater(state.last_successful_cutoff_at, watermark)

    @override_settings(SYNCER_DISCOVERY_LOOKBACK_MINUTES=10, SYNCER_DISCOVERY_OVERLAP_SECONDS=300)
    @mock.patch("syncer.tasks.sync_tasks.repo_advisory_lock")
    @mock.patch("syncer.tasks.sync_tasks.enqueue_with_parent")
    @mock.patch("syncer.tasks.sync_tasks.sync_pr_task")
    @mock.patch("syncer.tasks.sync_tasks.GitHubClient")
    def test_fresh_success_does_not_regress_watermark(self, MockClient, mock_sync_pr, mock_enqueue, mock_lock) -> None:
        mock_lock.return_value.__enter__.return_value = True
        old_watermark = timezone.now() - timedelta(minutes=30)
        RepoDiscoveryState.objects.create(repository=self.repo, last_successful_cutoff_at=old_watermark)
        gh = MockClient.return_value
        gh.get_last_rate_limit.return_value = {"remaining": 9999, "resetAt": "2030-01-01T00:00:00Z", "cost": 1}
        gh.discover_changed_pr_numbers.return_value = mock.Mock(
            numbers=[],
            reached_cutoff=True,
            next_cursor=None,
            hit_limit=False,
        )

        sync_repo_since_task.apply(kwargs={"repo_id": self.repo.id}).get()
        state = RepoDiscoveryState.objects.get(repository=self.repo)
        self.assertGreater(state.last_successful_cutoff_at, old_watermark)

    @mock.patch("syncer.tasks.sync_tasks.repo_advisory_lock")
    @mock.patch("syncer.tasks.sync_tasks.enqueue_with_parent")
    @mock.patch("syncer.tasks.sync_tasks.sync_pr_task")
    @mock.patch("syncer.tasks.sync_tasks.GitHubClient")
    def test_continuation_cursor_failure_falls_back_to_fresh(self, MockClient, mock_sync_pr, mock_enqueue, mock_lock) -> None:
        mock_lock.return_value.__enter__.return_value = True
        cutoff = timezone.now() - timedelta(minutes=30)
        RepoDiscoveryState.objects.create(
            repository=self.repo,
            continuation_cutoff_at=cutoff,
            continuation_cursor="CUR-BAD",
            continuation_started_at=timezone.now() - timedelta(minutes=5),
        )
        gh = MockClient.return_value
        gh.get_last_rate_limit.return_value = {"remaining": 9999, "resetAt": "2030-01-01T00:00:00Z", "cost": 1}
        gh.discover_changed_pr_numbers.side_effect = [
            RuntimeError("Invalid cursor"),
            mock.Mock(numbers=[44], reached_cutoff=True, next_cursor=None, hit_limit=False),
        ]

        res = sync_repo_since_task.apply(kwargs={"repo_id": self.repo.id}).get()
        self.assertEqual(res["mode"], "fresh_recovery")
        calls = gh.discover_changed_pr_numbers.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].kwargs["after"], "CUR-BAD")
        self.assertIsNone(calls[1].kwargs["after"])
        state = RepoDiscoveryState.objects.get(repository=self.repo)
        self.assertIsNotNone(state.last_successful_cutoff_at)
        self.assertIsNone(state.continuation_cursor)
