from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from core.models import Repository
from syncer.models import PullRequest, CheckRun, StatusContext
from syncer.tasks.sync_tasks import refresh_pending_ci_for_repo_task, sync_ci_for_shas_task
from syncer.tests.factories import make_repo, make_pr


class TestRefreshPendingCITask(TestCase):
    def setUp(self) -> None:
        self.repo: Repository = make_repo()
        self.pr: PullRequest = make_pr(self.repo, 1)

    def _make_checkrun(
        self,
        *,
        status: str = "IN_PROGRESS",
        head_sha: str = "sha1",
        started_at_delta_hours: int = 1,
        last_synced_at_delta_hours: int | None = None,
    ) -> CheckRun:
        now = timezone.now()
        cr = CheckRun.objects.create(
            pull_request=self.pr,
            github_node_id="CR1",
            head_sha=head_sha,
            name="ci/test",
            status=status,
            conclusion=None,
            details_url=None,
            external_id=None,
            gh_started_at=now - timedelta(hours=started_at_delta_hours),
            gh_completed_at=None,
            last_synced_at=(
                now - timedelta(hours=last_synced_at_delta_hours) if last_synced_at_delta_hours is not None else None
            ),
        )
        return cr

    def _make_status(
        self,
        *,
        state: str = "PENDING",
        head_sha: str = "sha1",
        created_delta_hours: int = 1,
        last_synced_at_delta_hours: int | None = None,
    ) -> StatusContext:
        now = timezone.now()
        sc = StatusContext.objects.create(
            pull_request=self.pr,
            github_node_id="SC1",
            rest_id=None,
            head_sha=head_sha,
            name="bors",
            state=state,
            target_url=None,
            description="",
            gh_created_at=now - timedelta(hours=created_delta_hours),
            last_synced_at=(
                now - timedelta(hours=last_synced_at_delta_hours) if last_synced_at_delta_hours is not None else None
            ),
        )
        return sc

    def test_skips_when_no_pending_ci(self) -> None:
        # No CheckRuns/StatusContexts at all: nothing to enqueue.
        res = refresh_pending_ci_for_repo_task(self.repo.id, max_prs=10, max_shas_per_pr=5, max_pending_hours=24)
        self.assertEqual(res.get("prs_enqueued"), 0)
        self.assertEqual(res.get("shas_enqueued"), 0)

    @mock.patch("syncer.tasks.sync_tasks.sync_ci_for_shas_task")
    def test_enqueues_for_recent_pending_ci(self, mock_sync_ci_for_shas) -> None:
        # One PR with a pending CheckRun that has never been explicitly synced.
        self._make_checkrun(status="IN_PROGRESS", head_sha="shaA", started_at_delta_hours=1, last_synced_at_delta_hours=None)

        # Avoid hitting the real broker; run task in-process and assert we request CI for the expected SHA.
        mock_sync_ci_for_shas.delay.return_value.id = "task-1"

        res = refresh_pending_ci_for_repo_task(self.repo.id, max_prs=10, max_shas_per_pr=5, max_pending_hours=24)
        self.assertEqual(res.get("prs_enqueued"), 1)
        self.assertEqual(res.get("shas_enqueued"), 1)
        items = res.get("items") or []
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["number"], 1)
        self.assertEqual(items[0]["shas"], ["shaA"])
        self.assertTrue(items[0]["task_id"])
        mock_sync_ci_for_shas.delay.assert_called_once()

    @mock.patch("syncer.tasks.sync_tasks.sync_ci_for_shas_task")
    def test_timeout_excludes_old_pending_ci(self, mock_sync_ci_for_shas) -> None:
        # Pending CheckRun whose pending_duration exceeds max_pending_hours should be skipped.
        # Origin is taken from gh_started_at; last_synced_at is far enough after origin.
        self._make_checkrun(
            status="IN_PROGRESS",
            head_sha="shaB",
            started_at_delta_hours=48,
            last_synced_at_delta_hours=24,
        )

        res = refresh_pending_ci_for_repo_task(self.repo.id, max_prs=10, max_shas_per_pr=5, max_pending_hours=12)
        self.assertEqual(res.get("prs_enqueued"), 0)
        self.assertEqual(res.get("shas_enqueued"), 0)
        mock_sync_ci_for_shas.delay.assert_not_called()
