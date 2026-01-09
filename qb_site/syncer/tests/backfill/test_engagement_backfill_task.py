from __future__ import annotations

from unittest import mock

from django.test import TestCase
from django.utils import timezone

from core.models import Repository
from syncer.models import PullRequest
from syncer.tasks.backfill_tasks import backfill_repo_engagement_task
from syncer.tests.factories import make_repo, make_pr


class TestEngagementBackfillTask(TestCase):
    def setUp(self) -> None:
        self.repo: Repository = make_repo()

    def _make_pr(
        self,
        number: int,
        *,
        state: str = "open",
        engagement_synced: bool = False,
        head_ci_state: str | None = None,
        head_sha: str | None = "a" * 40,
    ) -> PullRequest:
        last_synced = timezone.now() if engagement_synced else None
        pr = make_pr(self.repo, number, state=state, last_synced_at=last_synced, head_sha=head_sha)
        if engagement_synced:
            pr.engagement_synced_at = pr.last_synced_at or timezone.now()
        if head_ci_state is not None:
            pr.head_ci_state = head_ci_state
        if engagement_synced or head_ci_state is not None:
            pr.save(update_fields=["engagement_synced_at", "head_ci_state"])
        return pr

    @mock.patch("syncer.tasks.backfill_tasks.sync_pr_task")
    def test_skips_when_no_candidates(self, mock_sync_pr_task) -> None:
        self._make_pr(1, engagement_synced=True, head_ci_state="SUCCESS")

        res = backfill_repo_engagement_task(self.repo.id, limit=10)

        self.assertEqual(res.get("repo"), f"{self.repo.owner}/{self.repo.name}")
        self.assertEqual(res.get("enqueued"), 0)
        self.assertEqual(res.get("remaining"), 0)
        self.assertEqual(res.get("states"), ["OPEN", "MERGED", "CLOSED"])

    @mock.patch("syncer.tasks.backfill_tasks.sync_pr_task")
    def test_enqueues_missing_engagement_up_to_limit(self, mock_sync_pr_task) -> None:
        # One up-to-date PR and two needing engagement.
        self._make_pr(1, engagement_synced=True, head_ci_state="SUCCESS")
        needs1 = self._make_pr(2)
        needs2 = self._make_pr(3)

        res = backfill_repo_engagement_task(self.repo.id, limit=1)

        self.assertEqual(res.get("enqueued"), 1)
        self.assertEqual(res.get("remaining"), 1)
        self.assertEqual(mock_sync_pr_task.delay.call_count, 1)
        # Should have targeted one of the two needing engagement.
        enqueued_numbers = {call.args[1] for call in mock_sync_pr_task.delay.call_args_list}
        self.assertTrue(enqueued_numbers.issubset({needs1.number, needs2.number}))

    @mock.patch("syncer.tasks.backfill_tasks.sync_pr_task")
    def test_state_filter_respected(self, mock_sync_pr_task) -> None:
        self._make_pr(1, state="open")
        self._make_pr(2, state="closed")

        res_open_only = backfill_repo_engagement_task(self.repo.id, limit=10, states=["OPEN"])
        self.assertEqual(res_open_only.get("enqueued"), 1)
        self.assertEqual(res_open_only.get("remaining"), 0)
        self.assertEqual(res_open_only.get("states"), ["OPEN"])
        self.assertEqual(mock_sync_pr_task.delay.call_count, 1)

        mock_sync_pr_task.delay.reset_mock()

        res_closed = backfill_repo_engagement_task(self.repo.id, limit=10, states=["CLOSED", "MERGED"])
        self.assertEqual(res_closed.get("enqueued"), 1)
        self.assertEqual(res_closed.get("remaining"), 0)
        self.assertEqual(res_closed.get("states"), ["CLOSED", "MERGED"])
        self.assertEqual(mock_sync_pr_task.delay.call_count, 1)

    @mock.patch("syncer.tasks.backfill_tasks.sync_pr_task")
    def test_prefers_open_prs(self, mock_sync_pr_task) -> None:
        open_pr = self._make_pr(1, state="open")
        closed_pr = self._make_pr(2, state="closed")

        res = backfill_repo_engagement_task(self.repo.id, limit=1, states=["OPEN", "CLOSED"])

        self.assertEqual(res.get("enqueued"), 1)
        mock_sync_pr_task.delay.assert_called_once_with(self.repo.id, open_pr.number)

    @mock.patch("syncer.tasks.backfill_tasks.sync_pr_task")
    def test_includes_missing_head_ci_state(self, mock_sync_pr_task) -> None:
        # Engagement synced but head_ci_state missing should still be enqueued.
        pr = self._make_pr(1, engagement_synced=True, head_ci_state=None)

        res = backfill_repo_engagement_task(self.repo.id, limit=10)

        self.assertEqual(res.get("enqueued"), 1)
        self.assertEqual(res.get("remaining"), 0)
        mock_sync_pr_task.delay.assert_called_once_with(self.repo.id, pr.number)

    @mock.patch("syncer.tasks.backfill_tasks.sync_pr_task")
    def test_includes_missing_head_sha(self, mock_sync_pr_task) -> None:
        pr = self._make_pr(1, engagement_synced=True, head_ci_state="SUCCESS", head_sha=None)

        res = backfill_repo_engagement_task(self.repo.id, limit=10)

        self.assertEqual(res.get("enqueued"), 1)
        self.assertEqual(res.get("remaining"), 0)
        mock_sync_pr_task.delay.assert_called_once_with(self.repo.id, pr.number)
