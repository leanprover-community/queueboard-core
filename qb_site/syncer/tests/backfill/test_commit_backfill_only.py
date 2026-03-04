from __future__ import annotations

from unittest import mock

from django.test import TestCase
from django.utils import timezone
from django.conf import settings

from syncer.models.check_run import CheckRun
from syncer.models.commit_check_run import CommitCheckRun
from syncer.models.commit_status_context import CommitStatusContext
from syncer.models.status_context import StatusContext
from syncer.tasks.sync_tasks import sync_pr_task
from syncer.tests.factories import make_repo, make_pr


class TestCommitBackfillOnly(TestCase):
    def setUp(self) -> None:
        self.repo = make_repo()

    def _mk_pr(self, number: int, last_synced_at=None):
        if last_synced_at is None:
            last_synced_at = timezone.now()
        pr = make_pr(self.repo, number, last_synced_at=last_synced_at)
        pr.engagement_synced_at = last_synced_at
        pr.head_ci_state = "SUCCESS"
        pr.save(update_fields=["engagement_synced_at", "head_ci_state"])
        return pr

    @mock.patch("syncer.tasks.sync_tasks.GitHubClient")
    def test_commit_backfill_runs_when_up_to_date(self, MockClient) -> None:
        pr = self._mk_pr(11, last_synced_at=timezone.now())
        # Seed a saved cursor so subsequent runs continue from it
        pr.commits_backfill_cursor = "CURX0"
        pr.commits_backfill_done = False
        pr.save(update_fields=["commits_backfill_cursor", "commits_backfill_done"])
        gh = MockClient.return_value

        # Header older than last_synced_at → up-to-date path
        eps = int(getattr(settings, "SYNCER_LAST_SYNC_EPSILON_SECONDS", 2))
        gh.get_pr_header.return_value = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "number": 11,
                        "updatedAt": (pr.last_synced_at - timezone.timedelta(seconds=eps + 1)).isoformat(),
                    }
                }
            }
        }
        gh.get_last_rate_limit.return_value = {"remaining": 4990, "cost": 1, "resetAt": "2030-01-01T00:00:00Z"}

        # One commits page with a single commit containing both a CheckRun and a StatusContext
        # Capture and assert that we seed from the saved cursor
        commits_calls: list[dict] = []

        def _get_commits_page(**kwargs):  # type: ignore[no-redef]
            commits_calls.append(kwargs)
            # First call should start from the saved cursor
            if len(commits_calls) == 1:
                assert kwargs.get("before") == "CURX0"
            return {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "commits": {
                                "pageInfo": {"hasPreviousPage": False, "startCursor": "CURC"},
                                "nodes": [
                                    {
                                        "commit": {
                                            "oid": "abc123",
                                            "statusCheckRollup": {
                                                "contexts": {
                                                    "nodes": [
                                                        {
                                                            "__typename": "CheckRun",
                                                            "id": "CR1",
                                                            "name": "ci/test",
                                                            "status": "COMPLETED",
                                                            "conclusion": "SUCCESS",
                                                            "startedAt": "2024-01-01T00:00:00Z",
                                                            "completedAt": "2024-01-01T00:05:00Z",
                                                            "detailsUrl": None,
                                                            "externalId": None,
                                                        },
                                                        {
                                                            "__typename": "StatusContext",
                                                            "id": "SC1",
                                                            "context": "lint",
                                                            "state": "SUCCESS",
                                                            "targetUrl": None,
                                                            "description": "ok",
                                                            "createdAt": "2024-01-01T00:01:00Z",
                                                        },
                                                    ]
                                                }
                                            },
                                        }
                                    }
                                ],
                            }
                        }
                    }
                }
            }

        gh.get_commits_page.side_effect = _get_commits_page

        res = sync_pr_task.apply(
            kwargs={
                "repo_id": self.repo.id,
                "number": 11,
                "backfill_commit_pages": 1,
                "commitsM": 1,
            }
        ).get()

        self.assertFalse(res.get("skipped"))
        self.assertEqual(res.get("status"), "backfill_only")
        # CI rows should be created for the commit SHA
        self.assertEqual(CheckRun.objects.filter(pull_request=pr, head_sha="abc123").count(), 0)
        self.assertEqual(StatusContext.objects.filter(pull_request=pr, head_sha="abc123").count(), 0)
        self.assertGreaterEqual(CommitCheckRun.objects.filter(repository=self.repo, head_sha="abc123").count(), 1)
        self.assertGreaterEqual(CommitStatusContext.objects.filter(repository=self.repo, head_sha="abc123").count(), 1)
        # PR backfill admin fields updated
        pr.refresh_from_db()
        self.assertTrue(pr.commits_backfill_cursor)
        self.assertIsNotNone(pr.commits_earliest_synced_at)
        # And we used the saved cursor to seed
        self.assertGreaterEqual(len(commits_calls), 1)

    @mock.patch("syncer.tasks.sync_tasks.GitHubClient")
    def test_commit_backfill_skips_when_done(self, MockClient) -> None:
        pr = self._mk_pr(12, last_synced_at=timezone.now())
        pr.commits_backfill_cursor = "CURX0"
        pr.commits_backfill_done = True
        pr.save(update_fields=["commits_backfill_cursor", "commits_backfill_done"])
        gh = MockClient.return_value

        # Header older than last_synced_at → up-to-date path
        eps = int(getattr(settings, "SYNCER_LAST_SYNC_EPSILON_SECONDS", 2))
        gh.get_pr_header.return_value = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "number": 12,
                        "updatedAt": (pr.last_synced_at - timezone.timedelta(seconds=eps + 1)).isoformat(),
                    }
                }
            }
        }
        gh.get_last_rate_limit.return_value = {"remaining": 4990, "cost": 1, "resetAt": "2030-01-01T00:00:00Z"}

        # Commit backfill should be skipped entirely when commits_backfill_done is already True
        gh.get_commits_page.side_effect = AssertionError("commit backfill should not run when already done")

        res = sync_pr_task.apply(
            kwargs={
                "repo_id": self.repo.id,
                "number": 12,
                "backfill_commit_pages": 1,
                "commitsM": 1,
            }
        ).get()

        # Still treated as an up-to-date run
        self.assertTrue(res.get("skipped"))
        self.assertEqual(res.get("reason"), "up_to_date")
        # And commit backfill fields remain unchanged
        pr.refresh_from_db()
        self.assertTrue(pr.commits_backfill_done)
        self.assertEqual(pr.commits_backfill_cursor, "CURX0")
