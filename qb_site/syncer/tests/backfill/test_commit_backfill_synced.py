from __future__ import annotations

from unittest import mock

from django.test import TestCase

from core.models import Repository
from syncer.models import PullRequest
from syncer.models.check_run import CheckRun
from syncer.models.status_context import StatusContext
from syncer.services.pr_sync_service import PRSyncService
from syncer.tests.factories import make_repo, make_pr


class TestCommitBackfillSynced(TestCase):
    def setUp(self) -> None:
        self.repo = make_repo()
        # Seed a minimal PR row so service can look it up by number
        make_pr(
            self.repo,
            101,
            author=None,
            state="open",
            is_draft=False,
            gh_created_at="2024-01-01T00:00:00Z",
            gh_updated_at="2024-01-01T00:00:00Z",
            base_ref_name="master",
            head_ref_name="b",
            head_repo_owner_login="o",
            head_repo_name="fork",
            title="t",
            body="",
            additions=0,
            deletions=0,
            changed_files_count=0,
        )

    @mock.patch("syncer.services.pr_sync_service.GitHubClient")
    def test_commit_backfill_budget_runs_on_synced(self, MockClient) -> None:
        svc = PRSyncService()
        gh = MockClient.return_value

        # Bundle with commits pageInfo having a startCursor to seed backfill, no nodes on first page
        gh.get_pr_bundle.return_value = {
            "data": {
                "repository": {
                    "id": "R_repo",
                    "name": "r",
                    "owner": {"login": "o"},
                    "defaultBranchRef": {"name": "master"},
                    "pullRequest": {
                        "timelineItems": {"pageInfo": {"hasPreviousPage": False, "startCursor": None}, "nodes": []},
                        "commits": {"pageInfo": {"hasPreviousPage": True, "startCursor": "CUR0"}, "nodes": []},
                    },
                }
            }
        }

        # One older commits page returned by backfill with contexts
        gh.get_commits_page.return_value = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "commits": {
                            "pageInfo": {"hasPreviousPage": False, "startCursor": "CUR-1"},
                            "nodes": [
                                {
                                    "commit": {
                                        "oid": "def456",
                                        "statusCheckRollup": {
                                            "contexts": {
                                                "nodes": [
                                                    {
                                                        "__typename": "CheckRun",
                                                        "id": "CR2",
                                                        "name": "ci/test",
                                                        "status": "COMPLETED",
                                                        "conclusion": "SUCCESS",
                                                        "startedAt": "2024-02-01T00:00:00Z",
                                                        "completedAt": "2024-02-01T00:05:00Z",
                                                        "detailsUrl": None,
                                                        "externalId": None,
                                                    },
                                                    {
                                                        "__typename": "StatusContext",
                                                        "id": "SC2",
                                                        "context": "lint",
                                                        "state": "SUCCESS",
                                                        "targetUrl": None,
                                                        "description": "ok",
                                                        "createdAt": "2024-02-01T00:01:00Z",
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

        svc.sync_pull_request(
            self.repo,
            number=101,
            client=gh,  # type: ignore[arg-type]
            timelineK=1,
            commitsM=1,
            max_timeline_pages=0,
            max_commit_pages=0,
            backfill_timeline_pages=0,
            backfill_commit_pages=1,
        )

        pr = PullRequest.objects.get(repository=self.repo, number=101)
        self.assertTrue(pr.commits_backfill_cursor)
        self.assertTrue(pr.commits_backfill_done)
        self.assertGreaterEqual(CheckRun.objects.filter(pull_request=pr, head_sha="def456").count(), 1)
        self.assertGreaterEqual(StatusContext.objects.filter(pull_request=pr, head_sha="def456").count(), 1)
