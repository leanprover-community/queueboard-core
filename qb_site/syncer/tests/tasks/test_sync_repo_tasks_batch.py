from __future__ import annotations

from unittest import mock

from django.test import TestCase, override_settings

from syncer.models import RepoDiscoveryState
from syncer.tasks.sync_tasks import sync_repo_since_task
from syncer.tests.factories import make_repo


class TestSyncRepoTasksBatching(TestCase):
    def setUp(self) -> None:
        self.repo = make_repo()

    @override_settings(SYNCER_RATE_REMAINING_MIN=200, SYNCER_REPO_ENQUEUE_BATCH_MAX=30, SYNCER_EST_COST_PER_PR=150)
    @mock.patch("syncer.tasks.sync_tasks.debounce_repo_schedule", return_value=True)
    @mock.patch("syncer.tasks.sync_tasks.repo_advisory_lock")
    @mock.patch("syncer.tasks.sync_tasks.claim_enqueue_slot", return_value=True)
    @mock.patch("syncer.tasks.sync_tasks.sync_pr_task")
    @mock.patch("syncer.tasks.sync_tasks.enqueue_with_parent")
    @mock.patch("syncer.tasks.sync_tasks.GitHubClient")
    def test_batch_cap_holds_watermark_and_schedules_drain(
        self, MockClient, mock_enqueue, mock_sync_pr, _mock_claim, mock_lock, _mock_debounce
    ) -> None:
        mock_lock.return_value.__enter__.return_value = True
        gh = MockClient.return_value
        # 10 candidates discovered, scan reaches cutoff.
        gh.discover_changed_pr_numbers.return_value = mock.Mock(
            numbers=list(range(1, 11)),
            reached_cutoff=True,
            next_cursor=None,
            hit_limit=False,
        )
        # remaining 1000, threshold 200 -> allowed=800 -> dynamic_cap=5
        gh.get_last_rate_limit.return_value = {"remaining": 1000, "resetAt": "2030-01-01T00:00:00Z"}

        res = sync_repo_since_task.apply(kwargs={"repo_id": self.repo.id}).get()

        # Only 5 enqueued now; the other 5 are explicitly undrained, not silently dropped.
        self.assertEqual(res.get("enqueued"), 5)
        self.assertEqual(res.get("undrained"), 5)
        self.assertFalse(res.get("low_budget"))
        self.assertTrue(res.get("scan_complete"))
        # A near-term drain continuation is scheduled to pick up the tail.
        self.assertTrue(res.get("continuation_scheduled"))
        self.assertEqual(res.get("continuation_reason"), "undrained_tail")
        # Crucially, the watermark is NOT advanced past the undrained numbers.
        state = RepoDiscoveryState.objects.get(repository=self.repo)
        self.assertIsNone(state.last_successful_cutoff_at)
        # 5 PR enqueues + 1 continuation enqueue.
        self.assertEqual(mock_enqueue.call_count, 6)

    @override_settings(SYNCER_RATE_REMAINING_MIN=200, SYNCER_REPO_ENQUEUE_BATCH_MAX=30, SYNCER_EST_COST_PER_PR=150)
    @mock.patch("syncer.tasks.sync_tasks.debounce_repo_schedule", return_value=True)
    @mock.patch("syncer.tasks.sync_tasks.repo_advisory_lock")
    @mock.patch("syncer.tasks.sync_tasks.sync_pr_task")
    @mock.patch("syncer.tasks.sync_tasks.enqueue_with_parent")
    @mock.patch("syncer.tasks.sync_tasks.GitHubClient")
    @mock.patch("syncer.tasks.sync_tasks.claim_enqueue_slot")
    def test_dedupe_skips_do_not_consume_budget(
        self, mock_claim, MockClient, mock_enqueue, mock_sync_pr, mock_lock, _mock_debounce
    ) -> None:
        """Already-in-flight (dedupe-skipped) numbers must not burn budget slots, so a
        capped batch still reaches *new* numbers further down the list."""
        mock_lock.return_value.__enter__.return_value = True
        gh = MockClient.return_value
        gh.discover_changed_pr_numbers.return_value = mock.Mock(
            numbers=[1, 2, 3, 4],
            reached_cutoff=True,
            next_cursor=None,
            hit_limit=False,
        )
        # remaining 500 -> allowed=300 -> dynamic_cap=2 (budget of 2 new enqueues).
        gh.get_last_rate_limit.return_value = {"remaining": 500, "resetAt": "2030-01-01T00:00:00Z"}

        # PRs 1 and 2 are already in flight (dedupe-claimed); 3 and 4 are new.
        already_in_flight = set()

        def claim(*, key, ttl_seconds):
            if key.endswith(":1") or key.endswith(":2"):
                already_in_flight.add(key)
                return False
            return True

        mock_claim.side_effect = claim

        res = sync_repo_since_task.apply(kwargs={"repo_id": self.repo.id}).get()

        # Budget of 2 was spent on the two NEW numbers (3, 4), not wasted on 1/2.
        self.assertEqual(res.get("enqueued"), 2)
        self.assertEqual(res.get("prs_skipped_dedupe"), 2)
        self.assertEqual(res.get("undrained"), 0)
        enqueued_numbers = {call.args[1] for call in mock_sync_pr.s.call_args_list}
        self.assertEqual(enqueued_numbers, {3, 4})
        # Everything was covered, so the watermark advances.
        state = RepoDiscoveryState.objects.get(repository=self.repo)
        self.assertIsNotNone(state.last_successful_cutoff_at)

    @override_settings(SYNCER_RATE_REMAINING_MIN=200, SYNCER_REPO_ENQUEUE_BATCH_MAX=30, SYNCER_EST_COST_PER_PR=150)
    @mock.patch("syncer.tasks.sync_tasks.debounce_repo_schedule", return_value=True)
    @mock.patch("syncer.tasks.sync_tasks.repo_advisory_lock")
    @mock.patch("syncer.tasks.sync_tasks.sync_pr_task")
    @mock.patch("syncer.tasks.sync_tasks.enqueue_with_parent")
    @mock.patch("syncer.tasks.sync_tasks.GitHubClient")
    @mock.patch("syncer.tasks.sync_tasks.claim_enqueue_slot")
    def test_drain_completes_over_ticks_then_advances_watermark(
        self, mock_claim, MockClient, mock_enqueue, mock_sync_pr, mock_lock, _mock_debounce
    ) -> None:
        """A burst of 5 PRs with a budget of 2 drains over successive ticks; the watermark
        only advances once every discovered number has been covered."""
        mock_lock.return_value.__enter__.return_value = True
        gh = MockClient.return_value
        # The same window is rediscovered each tick (watermark held until fully drained).
        gh.discover_changed_pr_numbers.return_value = mock.Mock(
            numbers=[1, 2, 3, 4, 5],
            reached_cutoff=True,
            next_cursor=None,
            hit_limit=False,
        )
        gh.get_last_rate_limit.return_value = {"remaining": 500, "resetAt": "2030-01-01T00:00:00Z"}

        # Stateful enqueue dedupe: once a number is claimed it stays claimed for the run.
        claimed: set[str] = set()

        def claim(*, key, ttl_seconds):
            if key in claimed:
                return False
            claimed.add(key)
            return True

        mock_claim.side_effect = claim

        # Tick 1: enqueue 1,2 -> undrained 3, watermark held.
        res1 = sync_repo_since_task.apply(kwargs={"repo_id": self.repo.id}).get()
        self.assertEqual(res1.get("enqueued"), 2)
        self.assertEqual(res1.get("undrained"), 3)
        self.assertIsNone(RepoDiscoveryState.objects.get(repository=self.repo).last_successful_cutoff_at)

        # Tick 2: 1,2 dedupe-skipped, enqueue 3,4 -> undrained 1, watermark still held.
        res2 = sync_repo_since_task.apply(kwargs={"repo_id": self.repo.id}).get()
        self.assertEqual(res2.get("enqueued"), 2)
        self.assertEqual(res2.get("undrained"), 1)
        self.assertIsNone(RepoDiscoveryState.objects.get(repository=self.repo).last_successful_cutoff_at)

        # Tick 3: only 5 is new -> undrained 0, watermark finally advances.
        res3 = sync_repo_since_task.apply(kwargs={"repo_id": self.repo.id}).get()
        self.assertEqual(res3.get("enqueued"), 1)
        self.assertEqual(res3.get("undrained"), 0)
        self.assertIsNotNone(RepoDiscoveryState.objects.get(repository=self.repo).last_successful_cutoff_at)

        # All five distinct PRs were eventually enqueued exactly once.
        enqueued_numbers = {call.args[1] for call in mock_sync_pr.s.call_args_list}
        self.assertEqual(enqueued_numbers, {1, 2, 3, 4, 5})
