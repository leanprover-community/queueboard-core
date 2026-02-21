from __future__ import annotations

from unittest import mock

from django.test import TestCase
from django.utils import timezone

from core.models import Repository
from syncer.models import RepoBackfillCursor
from syncer.tasks.backfill_tasks import backfill_repo_history_task
from syncer.tests.factories import make_repo


class TestRepoHistoryBackfillTask(TestCase):
    def setUp(self) -> None:
        self.repo: Repository = make_repo()

    @mock.patch("syncer.tasks.backfill_tasks.sync_pr_task")
    @mock.patch("syncer.tasks.backfill_tasks.GitHubClient")
    def test_backfill_enqueues_prs_and_updates_cursor(self, MockClient, mock_sync_pr_task) -> None:
        gh = MockClient.return_value

        gh.get_prs_created_page.return_value = {
            "data": {
                "repository": {
                    "pullRequests": {
                        "pageInfo": {"hasNextPage": False, "endCursor": "CUR1"},
                        "nodes": [
                            {"number": 10, "createdAt": "2024-01-01T00:00:00Z", "state": "OPEN"},
                            {"number": 5, "createdAt": "2023-12-31T00:00:00Z", "state": "CLOSED"},
                        ],
                    }
                }
            }
        }

        res = backfill_repo_history_task(self.repo.id, page_size=50, max_pages=2)

        # Two PR sync tasks should be enqueued with the repo id and numbers 10 and 5
        self.assertFalse(res.get("skipped"))
        self.assertEqual(res.get("enqueued"), 2)
        self.assertTrue(res.get("completed"))
        self.assertEqual(mock_sync_pr_task.delay.call_count, 2)
        mock_sync_pr_task.delay.assert_any_call(self.repo.id, 10)
        mock_sync_pr_task.delay.assert_any_call(self.repo.id, 5)
        MockClient.assert_called_once_with(operation="syncer_repo_discovery", owner=self.repo.owner, repo=self.repo.name)

        cursor = RepoBackfillCursor.objects.get(repository=self.repo)
        self.assertTrue(cursor.completed)
        self.assertEqual(cursor.created_cursor, "CUR1")
        self.assertIsNotNone(cursor.oldest_created_at)
        self.assertLessEqual(cursor.oldest_created_at, timezone.now())

    @mock.patch("syncer.tasks.backfill_tasks.sync_pr_task")
    @mock.patch("syncer.tasks.backfill_tasks.GitHubClient")
    def test_backfill_continues_after_completed_when_new_prs_exist(self, MockClient, mock_sync_pr_task) -> None:
        # Pre-mark cursor as completed at an existing cursor; new PRs created after downtime
        cursor = RepoBackfillCursor.objects.create(repository=self.repo, created_cursor="CUR0", completed=True)
        gh = MockClient.return_value

        gh.get_prs_created_page.return_value = {
            "data": {
                "repository": {
                    "pullRequests": {
                        "pageInfo": {"hasNextPage": False, "endCursor": "CUR1"},
                        "nodes": [
                            {"number": 42, "createdAt": "2025-01-01T00:00:00Z", "state": "OPEN"},
                        ],
                    }
                }
            }
        }

        res = backfill_repo_history_task(self.repo.id, page_size=50, max_pages=1)

        self.assertFalse(res.get("skipped"))
        self.assertEqual(res.get("enqueued"), 1)
        self.assertTrue(res.get("completed"))
        mock_sync_pr_task.delay.assert_called_once_with(self.repo.id, 42)

        cursor.refresh_from_db()
        self.assertEqual(cursor.created_cursor, "CUR1")

    @mock.patch("syncer.tasks.backfill_tasks.sync_pr_task")
    @mock.patch("syncer.tasks.backfill_tasks.GitHubClient")
    def test_backfill_recovers_after_initial_empty_then_new_prs(self, MockClient, mock_sync_pr_task) -> None:
        gh = MockClient.return_value

        # First run: no PRs yet, repo appears empty
        gh.get_prs_created_page.side_effect = [
            {
                "data": {
                    "repository": {
                        "pullRequests": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [],
                        }
                    }
                }
            },
            {
                "data": {
                    "repository": {
                        "pullRequests": {
                            "pageInfo": {"hasNextPage": False, "endCursor": "CURX"},
                            "nodes": [
                                {"number": 7, "createdAt": "2025-03-01T00:00:00Z", "state": "OPEN"},
                            ],
                        }
                    }
                }
            },
        ]

        # Initial backfill sees no PRs but should mark cursor as completed
        res1 = backfill_repo_history_task(self.repo.id, page_size=50, max_pages=1)
        self.assertFalse(res1.get("skipped"))
        self.assertEqual(res1.get("enqueued"), 0)
        self.assertTrue(res1.get("completed"))
        cursor = RepoBackfillCursor.objects.get(repository=self.repo)
        self.assertTrue(cursor.completed)
        self.assertIsNone(cursor.created_cursor)

        # Later, a new PR appears; second run should still enqueue it even though completed was True
        res2 = backfill_repo_history_task(self.repo.id, page_size=50, max_pages=1)
        self.assertFalse(res2.get("skipped"))
        self.assertEqual(res2.get("enqueued"), 1)
        self.assertTrue(res2.get("completed"))
        mock_sync_pr_task.delay.assert_called_once_with(self.repo.id, 7)

        cursor.refresh_from_db()
        self.assertEqual(cursor.created_cursor, "CURX")
