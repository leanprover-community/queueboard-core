from __future__ import annotations

from unittest import mock

from django.test import TestCase

from syncer.tests.factories import make_repo, make_pr
from syncer.tasks.sync_tasks import sync_ci_for_repo_shas_task, sync_ci_for_shas_task


class TestSyncCIForSHAsTask(TestCase):
    def setUp(self) -> None:
        self.repo = make_repo(owner="o", name="r", default_branch="master", is_active=True)
        make_pr(
            self.repo,
            20,
            gh_created_at="2024-01-01T00:00:00Z",
            gh_updated_at="2024-01-02T00:00:00Z",
            base_ref_name="master",
            head_ref_name="b",
            head_repo_owner_login="o",
            head_repo_name="r",
        )

    @mock.patch("syncer.tasks.sync_tasks.run_ci_sync_for_pr_shas")
    @mock.patch("syncer.tasks.sync_tasks.GitHubClient")
    def test_processes_list_of_shas(self, MockClient, mock_runner):
        gh = MockClient.return_value
        gh.get_last_rate_limit.return_value = {"remaining": 4990, "resetAt": "2030-01-01T00:00:00Z"}
        mock_runner.return_value = {
            "status": "ok",
            "done": ["a", "b"],
            "counts": {"checkruns_created": 1, "checkruns_updated": 0, "status_created": 1, "status_updated": 0},
            "results_by_result": {"ok": 2},
            "per_sha_results": [],
            "per_sha_results_truncated": False,
            "remaining_shas": [],
            "reset_at": None,
        }

        res = sync_ci_for_shas_task.apply(
            kwargs={"repo_id": self.repo.id, "number": 20, "shas": ["a", "b"], "max_pages_per_sha": 1}
        ).get()
        self.assertEqual(res.get("status"), "ok")
        self.assertEqual(sorted(res.get("shas_done") or []), ["a", "b"])
        MockClient.assert_called_once_with(operation="syncer_ci_read", owner=self.repo.owner, repo=self.repo.name)

    @mock.patch("syncer.tasks.sync_tasks.run_ci_sync_for_pr_shas")
    @mock.patch("syncer.tasks.sync_tasks.GitHubClient")
    def test_defers_on_low_budget(self, MockClient, mock_runner):
        gh = MockClient.return_value
        gh.get_last_rate_limit.return_value = {"remaining": 0, "resetAt": "2030-01-01T00:00:00Z"}
        mock_runner.return_value = {
            "status": "deferred",
            "done": [],
            "counts": {"checkruns_created": 0, "checkruns_updated": 0, "status_created": 0, "status_updated": 0},
            "results_by_result": {},
            "per_sha_results": [],
            "per_sha_results_truncated": False,
            "remaining_shas": ["a", "b"],
            "reset_at": "2030-01-01T00:00:00Z",
        }
        res = sync_ci_for_shas_task.apply(
            kwargs={"repo_id": self.repo.id, "number": 20, "shas": ["a", "b"], "max_pages_per_sha": 1}
        ).get()
        self.assertEqual(res.get("status"), "deferred")

    @mock.patch("syncer.tasks.sync_tasks.run_ci_sync_for_pr_shas")
    @mock.patch("syncer.tasks.sync_tasks.GitHubClient")
    def test_repo_sha_task_aggregates_impacted_prs(self, MockClient, mock_runner):
        gh = MockClient.return_value
        gh.get_last_rate_limit.return_value = {"remaining": 4800, "resetAt": "2030-01-01T00:00:00Z"}
        make_pr(
            self.repo,
            21,
            gh_created_at="2024-01-01T00:00:00Z",
            gh_updated_at="2024-01-02T00:00:00Z",
            base_ref_name="master",
            head_ref_name="c",
            head_sha="sha1",
            head_repo_owner_login="o",
            head_repo_name="r",
        )
        make_pr(
            self.repo,
            22,
            gh_created_at="2024-01-01T00:00:00Z",
            gh_updated_at="2024-01-02T00:00:00Z",
            base_ref_name="master",
            head_ref_name="d",
            head_sha="sha1",
            head_repo_owner_login="o",
            head_repo_name="r",
        )
        mock_runner.return_value = {
            "status": "ok",
            "done": ["sha1"],
            "counts": {"checkruns_created": 1, "checkruns_updated": 0, "status_created": 0, "status_updated": 1},
            "results_by_result": {"ok": 1},
            "per_sha_results": [{"sha": "sha1", "result": "ok"}],
            "per_sha_results_truncated": False,
            "remaining_shas": [],
            "reset_at": None,
        }

        res = sync_ci_for_repo_shas_task.apply(kwargs={"repo_id": self.repo.id, "shas": ["sha1", "sha-missing"]}).get()

        self.assertEqual(res.get("status"), "ok")
        self.assertEqual(res.get("impacted_pr_count"), 2)
        self.assertEqual(res.get("unassociated_shas"), ["sha-missing"])
        self.assertEqual(mock_runner.call_count, 2)
