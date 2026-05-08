from __future__ import annotations

from unittest import mock

from django.test import TestCase

from syncer.models import PullRequest, PRTimelineEvent
from syncer.services.pr_sync_service import PRSyncService
from syncer.tasks.backfill_tasks import backfill_repo_incomplete_prs_task
from syncer.tests.factories import make_repo, make_pr


class TestTimelineBackfill(TestCase):
    def setUp(self) -> None:
        self.repo = make_repo()
        # Minimal PR row to target by number
        make_pr(
            self.repo,
            99,
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
                "inline_comments_created": 0,
                "inline_backfill_rows_upserted": 0,
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

    @mock.patch("syncer.services.pr_sync_service.GitHubClient")
    def test_backfill_seeds_when_cursor_missing(self, MockClient) -> None:
        svc = PRSyncService()
        gh = MockClient.return_value

        # Bundle with no startCursor but still indicating older pages exist.
        gh.get_pr_bundle.return_value = {
            "data": {
                "repository": {
                    "id": "R_repo",
                    "name": "r",
                    "owner": {"login": "o"},
                    "defaultBranchRef": {"name": "master"},
                    "pullRequest": {
                        "timelineItems": {"pageInfo": {"hasPreviousPage": True, "startCursor": None}, "nodes": []},
                        "commits": {"pageInfo": {"hasPreviousPage": False, "startCursor": None}, "nodes": []},
                    },
                }
            }
        }

        # First backfill call with before=None should return a page and set cursor
        gh.get_timeline_page_back.return_value = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "timelineItems": {
                            "pageInfo": {"hasPreviousPage": True, "startCursor": "CUR-SEED"},
                            "nodes": [
                                {
                                    "__typename": "LabeledEvent",
                                    "id": "e2",
                                    "createdAt": "2023-02-01T00:00:00Z",
                                    "label": {"name": "Y"},
                                },
                            ],
                        }
                    }
                }
            }
        }

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
                "inline_comments_created": 0,
                "inline_backfill_rows_upserted": 0,
            },
        ):
            svc.sync_pull_request(
                self.repo,
                number=99,
                client=gh,
                timelineK=2,
                commitsM=0,
                max_timeline_pages=0,
                max_commit_pages=0,
                backfill_timeline_pages=1,
            )

        pr = PullRequest.objects.get(repository=self.repo, number=99)
        self.assertEqual(pr.timeline_backfill_cursor, "CUR-SEED")

    @mock.patch("syncer.services.pr_sync_service.GitHubClient")
    def test_filtered_bundle_does_not_mark_backfill_done(self, MockClient) -> None:
        svc = PRSyncService()
        gh = MockClient.return_value

        # Bundle response simulating filtered timeline (since window) with no previous page.
        gh.get_pr_bundle.return_value = {
            "data": {
                "repository": {
                    "id": "R_repo",
                    "name": "r",
                    "owner": {"login": "o"},
                    "defaultBranchRef": {"name": "master"},
                    "pullRequest": {
                        "timelineItems": {"pageInfo": {"hasPreviousPage": False, "startCursor": "CUR0"}, "nodes": []},
                        "commits": {"pageInfo": {"hasPreviousPage": False, "startCursor": None}, "nodes": []},
                    },
                }
            }
        }

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
                "inline_comments_created": 0,
                "inline_backfill_rows_upserted": 0,
            },
        ):
            svc.sync_pull_request(
                self.repo,
                number=99,
                client=gh,
                timelineK=2,
                commitsM=0,
                max_timeline_pages=0,
                max_commit_pages=0,
                backfill_timeline_pages=0,
                timeline_since_iso_override="2024-01-01T00:00:00Z",
            )

        pr = PullRequest.objects.get(repository=self.repo, number=99)
        self.assertFalse(pr.timeline_backfill_done)
        self.assertEqual(pr.timeline_backfill_cursor, "CUR0")

    @mock.patch("syncer.services.pr_sync_service.GitHubClient")
    def test_unfiltered_bundle_marks_backfill_done_when_complete(self, MockClient) -> None:
        svc = PRSyncService()
        gh = MockClient.return_value

        gh.get_pr_bundle.return_value = {
            "data": {
                "repository": {
                    "id": "R_repo",
                    "name": "r",
                    "owner": {"login": "o"},
                    "defaultBranchRef": {"name": "master"},
                    "pullRequest": {
                        "timelineItems": {"pageInfo": {"hasPreviousPage": False, "startCursor": "CUR0"}, "nodes": []},
                        "commits": {"pageInfo": {"hasPreviousPage": False, "startCursor": None}, "nodes": []},
                    },
                }
            }
        }

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
                "inline_comments_created": 0,
                "inline_backfill_rows_upserted": 0,
            },
        ):
            svc.sync_pull_request(
                self.repo,
                number=99,
                client=gh,
                timelineK=2,
                commitsM=0,
                max_timeline_pages=0,
                max_commit_pages=0,
                backfill_timeline_pages=0,
            )

        pr = PullRequest.objects.get(repository=self.repo, number=99)
        self.assertTrue(pr.timeline_backfill_done)
        self.assertEqual(pr.timeline_backfill_cursor, "CUR0")

    @mock.patch("syncer.services.pr_sync_service.GitHubClient")
    def test_filtered_bundle_without_cursor_leaves_backfill_state_unset(self, MockClient) -> None:
        svc = PRSyncService()
        gh = MockClient.return_value

        gh.get_pr_bundle.return_value = {
            "data": {
                "repository": {
                    "id": "R_repo",
                    "name": "r",
                    "owner": {"login": "o"},
                    "defaultBranchRef": {"name": "master"},
                    "pullRequest": {
                        "timelineItems": {"pageInfo": {"hasPreviousPage": False, "startCursor": None}, "nodes": []},
                        "commits": {"pageInfo": {"hasPreviousPage": False, "startCursor": None}, "nodes": []},
                    },
                }
            }
        }

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
                "inline_comments_created": 0,
                "inline_backfill_rows_upserted": 0,
            },
        ):
            svc.sync_pull_request(
                self.repo,
                number=99,
                client=gh,
                timelineK=2,
                commitsM=0,
                max_timeline_pages=0,
                max_commit_pages=0,
                backfill_timeline_pages=0,
                timeline_since_iso_override="2024-01-01T00:00:00Z",
            )

        pr = PullRequest.objects.get(repository=self.repo, number=99)
        self.assertFalse(pr.timeline_backfill_done)
        self.assertIsNone(pr.timeline_backfill_cursor)

    @mock.patch("syncer.services.pr_sync_service.GitHubClient")
    def test_unfiltered_bundle_without_cursor_marks_backfill_done(self, MockClient) -> None:
        svc = PRSyncService()
        gh = MockClient.return_value

        gh.get_pr_bundle.return_value = {
            "data": {
                "repository": {
                    "id": "R_repo",
                    "name": "r",
                    "owner": {"login": "o"},
                    "defaultBranchRef": {"name": "master"},
                    "pullRequest": {
                        "timelineItems": {"pageInfo": {"hasPreviousPage": False, "startCursor": None}, "nodes": []},
                        "commits": {"pageInfo": {"hasPreviousPage": False, "startCursor": None}, "nodes": []},
                    },
                }
            }
        }

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
                "inline_comments_created": 0,
                "inline_backfill_rows_upserted": 0,
            },
        ):
            svc.sync_pull_request(
                self.repo,
                number=99,
                client=gh,
                timelineK=2,
                commitsM=0,
                max_timeline_pages=0,
                max_commit_pages=0,
                backfill_timeline_pages=0,
            )

        pr = PullRequest.objects.get(repository=self.repo, number=99)
        self.assertTrue(pr.timeline_backfill_done)
        self.assertIsNone(pr.timeline_backfill_cursor)

    @mock.patch("syncer.services.pr_sync_service.GitHubClient")
    def test_backfill_completes_after_filtered_sync(self, MockClient) -> None:
        svc = PRSyncService()
        gh = MockClient.return_value

        pr = PullRequest.objects.get(repository=self.repo, number=99)

        gh.get_pr_bundle.side_effect = [
            {
                "data": {
                    "repository": {
                        "id": "R_repo",
                        "name": "r",
                        "owner": {"login": "o"},
                        "defaultBranchRef": {"name": "master"},
                        "pullRequest": {
                            "timelineItems": {"pageInfo": {"hasPreviousPage": False, "startCursor": "CUR0"}, "nodes": []},
                            "commits": {"pageInfo": {"hasPreviousPage": False, "startCursor": None}, "nodes": []},
                        },
                    }
                }
            },
            {
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
            },
        ]

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
                                        "id": "e10",
                                        "createdAt": "2024-01-15T00:00:00Z",
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
                                    {"__typename": "ReadyForReviewEvent", "id": "e9", "createdAt": "2024-01-10T00:00:00Z"},
                                ],
                            }
                        }
                    }
                }
            },
        ]

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
                "inline_comments_created": 0,
                "inline_backfill_rows_upserted": 0,
            },
        ):
            svc.sync_pull_request(
                self.repo,
                number=99,
                client=gh,
                timelineK=2,
                commitsM=0,
                max_timeline_pages=0,
                max_commit_pages=0,
                backfill_timeline_pages=0,
                timeline_since_iso_override="2024-02-01T00:00:00Z",
            )

        pr.refresh_from_db()
        self.assertFalse(pr.timeline_backfill_done)
        self.assertEqual(pr.timeline_backfill_cursor, "CUR0")

        with mock.patch("syncer.tasks.backfill_tasks.sync_pr_task") as mock_sync_pr:
            res = backfill_repo_incomplete_prs_task(self.repo.id, limit=10)
            self.assertEqual(res.get("enqueued"), 1)
            mock_sync_pr.delay.assert_called_once_with(
                self.repo.id,
                pr.number,
                backfill_timeline_pages=mock.ANY,
                backfill_commit_pages=mock.ANY,
            )

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
                "inline_comments_created": 0,
                "inline_backfill_rows_upserted": 0,
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

        pr.refresh_from_db()
        self.assertTrue(pr.timeline_backfill_done)
        self.assertEqual(pr.timeline_backfill_cursor, "CUR-2")
        self.assertGreaterEqual(res.get("events_created", 0), 2)
        self.assertGreaterEqual(PRTimelineEvent.objects.filter(pull_request=pr).count(), 2)
