from __future__ import annotations

from unittest import mock

from django.test import TestCase

from core.models import Repository
from syncer.models import PullRequest, PRTimelineEvent
from syncer.services.pr_sync_service import PRSyncService


class TestTimelineBackfill(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="o", name="r", default_branch="master", is_active=True)
        # Minimal PR row to target by number
        PullRequest.objects.create(
            repository=self.repo,
            number=99,
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
    def test_backfill_pages_with_budget(self, MockClient) -> None:  # type: ignore[no-redef]
        svc = PRSyncService()
        gh = MockClient.return_value

        # Bundle response with timeline pageInfo including startCursor
        gh.get_pr_bundle.return_value = {
            "data": {
                "repository": {
                    "id": "R_repo",
                    "name": "r",
                    "owner": {"login": "o"},
                    "defaultBranchRef": {"name": "master"},
                    "pullRequest": {
                        "timelineItems": {"pageInfo": {"hasPreviousPage": True, "startCursor": "CUR0"}, "nodes": []},
                        "commits": {"pageInfo": {"hasPreviousPage": False, "startCursor": None}, "nodes": []},
                    },
                }
            }
        }

        # Two back pages
        gh.get_timeline_page_back.side_effect = [
            {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "timelineItems": {
                                "pageInfo": {"hasPreviousPage": True, "startCursor": "CUR-1"},
                                "nodes": [
                                    {
                                        "__typename": "LabeledEvent",
                                        "id": "e1",
                                        "createdAt": "2023-01-01T00:00:00Z",
                                        "label": {"name": "X"},
                                    },
                                ],
                            }
                        }
                    }
                }
            },
            {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "timelineItems": {
                                "pageInfo": {"hasPreviousPage": False, "startCursor": "CUR-2"},
                                "nodes": [
                                    {"__typename": "ReadyForReviewEvent", "id": "e0", "createdAt": "2022-12-31T23:00:00Z"},
                                ],
                            }
                        }
                    }
                }
            },
        ]

        # Stub out bundle ingestion to avoid touching other subsystems
        with mock.patch.object(
            PRSyncService,
            "sync_pull_request_bundle",
            return_value={
                "labels_created": 0,
                "labels_updated": 0,
                "prlabels_created": 0,
                "prlabels_deleted": 0,
                "events_created": 0,
                "checkruns_upserted": 0,
                "statusctx_upserted": 0,
            },
        ):
            res = svc.sync_pull_request(
                self.repo,
                number=99,
                client=gh,
                timelineK=2,
                commitsM=0,
                max_timeline_pages=0,
                max_commit_pages=0,
                backfill_timeline_pages=2,
            )

        # Two events were created from backfill
        self.assertGreaterEqual(res.get("events_created", 0), 2)
        pr = PullRequest.objects.get(repository=self.repo, number=99)
        self.assertTrue(pr.timeline_backfill_done)
        self.assertEqual(pr.timeline_backfill_cursor, "CUR-2")
        # Events persisted
        self.assertGreaterEqual(PRTimelineEvent.objects.filter(pull_request=pr).count(), 2)
