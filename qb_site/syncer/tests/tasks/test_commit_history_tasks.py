from __future__ import annotations

from django.test import TestCase
from django.utils import timezone
from unittest.mock import patch

from syncer.models import PullRequest, CommitHistoryHarvest
from core.models import Repository
from syncer.tasks.commit_history_tasks import harvest_commit_history_sweep, harvest_commit_history_task
from syncer.models import CheckRun, StatusContext


class TestCommitHistoryTasks(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="o", name="r", default_branch="master", is_active=True)
        self.pr = PullRequest.objects.create(
            repository=self.repo,
            number=1,
            author=None,
            state="open",
            is_draft=False,
            gh_created_at=self.repo.created_at,
            gh_updated_at=self.repo.created_at,
            base_ref_name="master",
            head_ref_name="branch",
            head_repo_owner_login="o",
            head_repo_name="r",
            title="t",
            body="",
            additions=0,
            deletions=0,
            changed_files_count=0,
            timeline_backfill_done=True,
        )

    def test_sweep_enqueues_harvest_tasks_with_cutoff(self) -> None:
        ch = CommitHistoryHarvest.objects.create(
            pull_request=self.pr,
            start_sha="abc123",
            has_more=True,
            cursor="cur",
        )
        with patch("syncer.tasks.commit_history_tasks.harvest_commit_history_task.delay") as mock_delay:
            res = harvest_commit_history_sweep(max_jobs=1, max_pages=1, page_size=10)
        self.assertFalse(res["truncated"])
        self.assertEqual(len(res["enqueued"]), 1)
        mock_delay.assert_called_once()
        kwargs = mock_delay.call_args.kwargs
        self.assertEqual(kwargs["pr_id"], self.pr.id)
        self.assertEqual(kwargs["start_sha"], "abc123")
        self.assertEqual(kwargs["max_pages"], 1)
        self.assertEqual(kwargs["page_size"], 10)

    def test_sweep_truncates(self) -> None:
        CommitHistoryHarvest.objects.create(pull_request=self.pr, start_sha="a", has_more=True)
        CommitHistoryHarvest.objects.create(pull_request=self.pr, start_sha="b", has_more=True)
        with patch("syncer.tasks.commit_history_tasks.harvest_commit_history_task.delay") as mock_delay:
            res = harvest_commit_history_sweep(max_jobs=1)
        self.assertTrue(res["truncated"])
        self.assertEqual(len(res["enqueued"]), 1)

    def test_harvest_task_enqueues_ci_for_missing(self) -> None:
        # If harvest returns shas and no CI exists, ensure enqueue happens
        state = CommitHistoryHarvest.objects.create(pull_request=self.pr, start_sha="sha1")
        with patch(
            "syncer.tasks.commit_history_tasks.harvest_commit_history_with_cursor",
            return_value=(["shaX"], state),
        ):
            with patch("syncer.tasks.commit_history_tasks.sync_ci_for_shas_task.delay") as mock_ci:
                res = harvest_commit_history_task(
                    pr_id=self.pr.id,
                    start_sha="sha1",
                    max_pages=1,
                    page_size=1,
                    since_iso=None,
                )
                mock_ci.assert_called_once()
                self.assertEqual(res["ci_missing"], ["shaX"])
                self.assertEqual(res["repo"], "o/r")
                self.assertEqual(res["number"], 1)

        # If CI already exists for the harvested sha, do not enqueue
        state2 = CommitHistoryHarvest.objects.create(pull_request=self.pr, start_sha="sha2")
        CheckRun.objects.create(
            pull_request=self.pr,
            github_node_id="CR_exists",
            head_sha="shaY",
            name="build",
            status="COMPLETED",
            conclusion="SUCCESS",
            details_url=None,
            external_id=None,
        )
        with patch(
            "syncer.tasks.commit_history_tasks.harvest_commit_history_with_cursor",
            return_value=(["shaY"], state2),
        ):
            with patch("syncer.tasks.commit_history_tasks.sync_ci_for_shas_task.delay") as mock_ci2:
                res = harvest_commit_history_task(
                    pr_id=self.pr.id,
                    start_sha="sha2",
                    max_pages=1,
                    page_size=1,
                    since_iso=None,
                )
                mock_ci2.assert_not_called()
                self.assertEqual(res["ci_missing"], [])
                self.assertEqual(res["repo"], "o/r")
                self.assertEqual(res["number"], 1)

    def test_harvest_task_enqueues_for_pending_ci_rows(self) -> None:
        state = CommitHistoryHarvest.objects.create(pull_request=self.pr, start_sha="sha3")
        StatusContext.objects.create(
            pull_request=self.pr,
            github_node_id="SC_pending",
            rest_id=None,
            head_sha="sha_pending",
            name="lint",
            state="PENDING",
            target_url=None,
            description=None,
            gh_created_at=timezone.now(),
        )
        CheckRun.objects.create(
            pull_request=self.pr,
            github_node_id="CR_queued",
            head_sha="sha_queued",
            name="build",
            status="QUEUED",
            conclusion=None,
            details_url=None,
            external_id=None,
        )
        StatusContext.objects.create(
            pull_request=self.pr,
            github_node_id="SC_done",
            rest_id=None,
            head_sha="sha_done",
            name="lint",
            state="SUCCESS",
            target_url=None,
            description=None,
            gh_created_at=timezone.now(),
        )

        with patch(
            "syncer.tasks.commit_history_tasks.harvest_commit_history_with_cursor",
            return_value=(["sha_pending", "sha_queued", "sha_done"], state),
        ):
            with patch("syncer.tasks.commit_history_tasks.sync_ci_for_shas_task.delay") as mock_ci:
                res = harvest_commit_history_task(
                    pr_id=self.pr.id,
                    start_sha="sha3",
                    max_pages=1,
                    page_size=1,
                    since_iso=None,
                )
        mock_ci.assert_called_once()
        self.assertEqual(set(res["ci_missing"]), {"sha_pending", "sha_queued"})
