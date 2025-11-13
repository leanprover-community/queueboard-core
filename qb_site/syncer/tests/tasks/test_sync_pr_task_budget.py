from __future__ import annotations

from unittest import mock

from django.test import TestCase

from core.models import Repository
from syncer.tasks.sync_tasks import sync_pr_task
from syncer.tests.factories import make_repo


class TestSyncPrTaskBudget(TestCase):
    def setUp(self) -> None:
        self.repo = make_repo()

    @mock.patch("syncer.tasks.sync_tasks.GitHubClient")
    def test_header_rate_limit_defers(self, MockClient) -> None:
        # Make apply_async on the real task observable via the proxy
        with mock.patch.object(sync_pr_task, "apply_async") as mock_apply:
            gh = MockClient.return_value
            # Header call raises (simulate rate-limit path); last snapshot shows 0 remaining
            gh.get_pr_header.side_effect = RuntimeError("GraphQL error(s): RATE_LIMITED")
            gh.get_last_rate_limit.return_value = {"remaining": 0, "resetAt": "2030-01-01T00:00:00Z"}

            res = sync_pr_task.apply(kwargs={"repo_id": self.repo.id, "number": 5}).get()
            self.assertTrue(res.get("skipped"))
            self.assertEqual(res.get("reason"), "deferred_low_budget")
            self.assertEqual(res.get("where"), "header")
            mock_apply.assert_called_once()
