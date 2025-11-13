from __future__ import annotations

from unittest import mock

from django.test import TestCase
from django.utils import timezone

from core.models import Repository
from syncer.models import PullRequest
from syncer.models.check_run import CheckRun
from syncer.models.status_context import StatusContext
from syncer.tasks.sync_tasks import sync_pr_task


class TestCommitBackfillOnly(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="o", name="r", default_branch="master", is_active=True)

    def _mk_pr(self, number: int, last_synced_at=None):
        if last_synced_at is None:
            last_synced_at = timezone.now()
        return PullRequest.objects.create(
            repository=self.repo,
            number=number,
            state="open",
            is_draft=False,
            gh_created_at=timezone.now(),
            gh_updated_at=timezone.now(),
            base_ref_name="master",
            head_ref_name="b",
            head_repo_owner_login="o",
            head_repo_name="fork",
            title="t",
            body="",
            additions=0,
            deletions=0,
            changed_files_count=0,
            last_synced_at=last_synced_at,
        )

    @mock.patch("syncer.tasks.sync_tasks.GitHubClient")
    def test_commit_backfill_runs_when_up_to_date(self, MockClient) -> None:
        pr = self._mk_pr(11, last_synced_at=timezone.now())
        gh = MockClient.return_value

        # Header older than last_synced_at → up-to-date path
        gh.get_pr_header.return_value = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "number": 11,
                        "updatedAt": (pr.last_synced_at - timezone.timedelta(seconds=1)).isoformat(),
                    }
                }
            }
        }
        gh.get_last_rate_limit.return_value = {"remaining": 4990, "cost": 1, "resetAt": "2030-01-01T00:00:00Z"}

        # One commits page with a single commit containing both a CheckRun and a StatusContext
        gh.get_commits_page.return_value = {
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
        # CI rows should be created for the PR
        self.assertGreaterEqual(CheckRun.objects.filter(pull_request=pr, head_sha="abc123").count(), 1)
        self.assertGreaterEqual(StatusContext.objects.filter(pull_request=pr, head_sha="abc123").count(), 1)
        # PR backfill admin fields updated
        pr.refresh_from_db()
        self.assertTrue(pr.commits_backfill_cursor)
        self.assertIsNotNone(pr.commits_earliest_synced_at)
