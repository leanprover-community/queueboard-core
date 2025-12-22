from __future__ import annotations

from unittest import mock

from django.test import TestCase
from django.utils import timezone

from core.models import Repository
from syncer.models import PullRequest
from syncer.tasks.backfill_tasks import backfill_repo_incomplete_prs_task
from syncer.tests.factories import make_repo, make_pr


class TestIncompletePrBackfillTask(TestCase):
    def setUp(self) -> None:
        self.repo: Repository = make_repo()

    def _make_pr(
        self,
        number: int,
        *,
        state: str = "open",
        timeline_done: bool = True,
        commits_done: bool = True,
    ) -> PullRequest:
        return make_pr(
            self.repo,
            number,
            state=state,
            timeline_backfill_done=timeline_done,
            commits_backfill_done=commits_done,
            last_synced_at=timezone.now(),
        )

    def test_skips_when_no_incomplete_prs(self) -> None:
        # All PRs are already fully backfilled.
        self._make_pr(1, timeline_done=True, commits_done=True)
        self._make_pr(2, timeline_done=True, commits_done=True)

        res = backfill_repo_incomplete_prs_task(self.repo.id, limit=10)

        self.assertEqual(res.get("repo"), f"{self.repo.owner}/{self.repo.name}")
        self.assertEqual(res.get("repo_id"), self.repo.id)
        self.assertEqual(res.get("enqueued"), 0)
        # remaining should reflect the number of incomplete PRs (zero)
        self.assertEqual(res.get("remaining"), 0)
        self.assertEqual(res.get("states"), ["OPEN", "MERGED", "CLOSED"])

    @mock.patch("syncer.tasks.backfill_tasks.sync_pr_task")
    def test_enqueues_only_incomplete_prs_up_to_limit(self, mock_sync_pr_task) -> None:
        # One complete PR and two incomplete PRs.
        complete_pr = self._make_pr(1, timeline_done=True, commits_done=True)
        incomplete1 = self._make_pr(2, timeline_done=False, commits_done=True)
        self._make_pr(3, timeline_done=True, commits_done=False)

        # Make one of the incomplete PRs appear more recently updated.
        incomplete1.gh_updated_at = complete_pr.gh_updated_at
        incomplete1.save(update_fields=["gh_updated_at"])

        # Use a low limit so we can assert that only a subset is enqueued.
        res = backfill_repo_incomplete_prs_task(self.repo.id, limit=1)

        self.assertEqual(res.get("repo_id"), self.repo.id)
        self.assertEqual(res.get("enqueued"), 1)
        # There were two incomplete PRs; after enqueuing one, one should remain.
        self.assertEqual(res.get("remaining"), 1)
        self.assertIn("OPEN", res.get("states") or [])
        mock_sync_pr_task.delay.assert_called_once()

    @mock.patch("syncer.tasks.backfill_tasks.sync_pr_task")
    def test_state_filter_maps_open_closed_and_merges(self, mock_sync_pr_task) -> None:
        # Open PRs (eligible when states include OPEN).
        self._make_pr(1, state="open", timeline_done=False, commits_done=False)
        # Closed PRs (eligible for CLOSED/MERGED).
        self._make_pr(2, state="closed", timeline_done=False, commits_done=False)

        # Restrict to OPEN only.
        res_open_only = backfill_repo_incomplete_prs_task(self.repo.id, limit=10, states=["OPEN"])
        self.assertEqual(res_open_only.get("states"), ["OPEN"])
        # Both PRs are incomplete, but only one matches the OPEN state.
        self.assertEqual(res_open_only.get("enqueued"), 1)
        self.assertEqual(res_open_only.get("remaining"), 0)
        self.assertEqual(mock_sync_pr_task.delay.call_count, 1)

        mock_sync_pr_task.delay.reset_mock()

        # Restrict to CLOSED and MERGED (both map to closed in our schema).
        res_closed = backfill_repo_incomplete_prs_task(self.repo.id, limit=10, states=["CLOSED", "MERGED"])
        # Two states collapse into a single DB state; both incomplete PRs should be considered
        # but only the closed PR should be enqueued.
        self.assertEqual(res_closed.get("states"), ["CLOSED", "MERGED"])
        self.assertEqual(res_closed.get("enqueued"), 1)
        self.assertEqual(res_closed.get("remaining"), 0)
        self.assertEqual(mock_sync_pr_task.delay.call_count, 1)

    @mock.patch("syncer.tasks.backfill_tasks.sync_pr_task")
    def test_includes_stale_last_synced_prs(self, mock_sync_pr_task) -> None:
        now = timezone.now()
        stale_pr = self._make_pr(4, timeline_done=True, commits_done=True)
        stale_pr.gh_updated_at = now
        stale_pr.last_synced_at = now - timezone.timedelta(minutes=10)
        stale_pr.save(update_fields=["gh_updated_at", "last_synced_at"])

        res = backfill_repo_incomplete_prs_task(self.repo.id, limit=10)

        self.assertEqual(res.get("enqueued"), 1)
        mock_sync_pr_task.delay.assert_called_once_with(
            self.repo.id,
            stale_pr.number,
            backfill_timeline_pages=mock.ANY,
            backfill_commit_pages=mock.ANY,
        )
