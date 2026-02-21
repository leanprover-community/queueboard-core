from __future__ import annotations

from unittest import mock

from django.test import TestCase

from syncer.tests.factories import make_repo, make_pr
from syncer.tasks.sync_tasks import sync_ci_for_shas_task


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

    @mock.patch("syncer.tasks.sync_tasks.sync_ci_for_sha")
    @mock.patch("syncer.tasks.sync_tasks.GitHubClient")
    def test_processes_list_of_shas(self, MockClient, mock_sync):
        gh = MockClient.return_value
        gh.get_rate_limit.return_value = {"remaining": 5000, "resetAt": "2030-01-01T00:00:00Z"}
        gh.get_last_rate_limit.return_value = {"remaining": 4990, "resetAt": "2030-01-01T00:00:00Z"}
        mock_sync.return_value = {"checkruns_created": 1, "checkruns_updated": 0, "status_created": 1, "status_updated": 0}

        res = sync_ci_for_shas_task.apply(
            kwargs={"repo_id": self.repo.id, "number": 20, "shas": ["a", "b"], "max_pages_per_sha": 1}
        ).get()
        self.assertEqual(res.get("status"), "ok")
        self.assertEqual(sorted(res.get("shas_done") or []), ["a", "b"])
        MockClient.assert_called_once_with(operation="syncer_ci_read", owner=self.repo.owner, repo=self.repo.name)

    @mock.patch("syncer.tasks.sync_tasks.GitHubClient")
    def test_defers_on_low_budget(self, MockClient):
        gh = MockClient.return_value
        gh.get_rate_limit.return_value = {"remaining": 0, "resetAt": "2030-01-01T00:00:00Z"}
        res = sync_ci_for_shas_task.apply(
            kwargs={"repo_id": self.repo.id, "number": 20, "shas": ["a", "b"], "max_pages_per_sha": 1}
        ).get()
        self.assertEqual(res.get("status"), "deferred")
