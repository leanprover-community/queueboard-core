"""Page-path inline-comments ingestion (regression for the v=2 wave gap).

The v=2 wave shipped with a wire-up gap: ``_sync_inline_review_comments``
was only invoked from the bundle path, so the forward (``get_timeline_page``)
and backward (``get_timeline_page_back``) loops persisted ``REVIEW_*`` /
``ISSUE_COMMENTED`` ``PRTimelineEvent`` rows but never wrote the nested
inline comments — even though the GraphQL response carried them. The fix
in :mod:`syncer.services.pr_sync_service` calls the inline-comments service
on every timeline page; these tests pin that invariant.

These tests bypass the bundle ingest by stubbing
:meth:`PRSyncService.sync_pull_request_bundle` to a no-op result, so the
only inline comments persisted come from the page loop under test.
"""

from __future__ import annotations

from unittest import mock

from django.test import TestCase

from syncer.models import PullRequest, PRTimelineEvent
from syncer.models.pr_review_inline_comment import (
    PRReviewInlineComment,
    PRReviewInlineCommentBackfill,
)
from syncer.models.pr_timeline_event import PRTimelineEventType
from syncer.services.pr_sync_service import PRSyncService
from syncer.tests.factories import make_pr, make_repo


_BUNDLE_NOOP_RESULT = {
    "labels_created": 0,
    "labels_updated": 0,
    "prlabels_created": 0,
    "prlabels_deleted": 0,
    "events_created": 0,
    "checkruns_upserted": 0,
    "statusctx_upserted": 0,
    "inline_comments_created": 0,
    "inline_backfill_rows_upserted": 0,
}


def _bundle_response(*, has_previous_page: bool = True, start_cursor: str | None = "CUR0") -> dict:
    """Minimal bundle response used to seed the page loops."""
    return {
        "data": {
            "repository": {
                "id": "R_repo",
                "name": "r",
                "owner": {"login": "o"},
                "defaultBranchRef": {"name": "master"},
                "pullRequest": {
                    "timelineItems": {
                        "pageInfo": {
                            "hasPreviousPage": has_previous_page,
                            "startCursor": start_cursor,
                            "hasNextPage": False,
                            "endCursor": None,
                        },
                        "nodes": [],
                    },
                    "commits": {
                        "pageInfo": {"hasPreviousPage": False, "startCursor": None},
                        "nodes": [],
                    },
                },
            }
        }
    }


def _review_node(
    *,
    review_id: str,
    submitted_at: str,
    state: str,
    inline_comments: list[dict],
    has_next_page: bool = False,
    total_count: int | None = None,
) -> dict:
    return {
        "__typename": "PullRequestReview",
        "id": review_id,
        "submittedAt": submitted_at,
        "state": state,
        "author": {"login": "reviewer-bob", "__typename": "User"},
        "comments": {
            "totalCount": total_count if total_count is not None else len(inline_comments),
            "pageInfo": {"hasNextPage": has_next_page},
            "nodes": inline_comments,
        },
    }


def _inline_comment(
    *,
    node_id: str,
    created_at: str = "2024-01-15T00:00:00Z",
    path: str = "src/foo.py",
    line: int = 1,
    reply_to: str | None = None,
    author: str = "reviewer-bob",
) -> dict:
    return {
        "id": node_id,
        "createdAt": created_at,
        "path": path,
        "line": line,
        "originalLine": line,
        "replyTo": {"id": reply_to} if reply_to else None,
        "author": {"login": author, "__typename": "User"},
    }


class TestBackfillPagePersistsInlineComments(TestCase):
    """Backward (`get_timeline_page_back`) page loop must persist inline comments.

    This is the loop driven by the v=2 / v=3 wave: ``UpgradeToV*.kick``
    enqueues ``sync_pr_task(force=True, backfill_timeline_pages=N)``, the
    task fetches back-pages, and the page loop is expected to ingest both
    the ``PullRequestReview`` events and their nested inline comments.
    """

    def setUp(self) -> None:
        self.repo = make_repo()
        make_pr(self.repo, 99)

    @mock.patch("syncer.services.pr_sync_service.GitHubClient")
    def test_back_page_creates_inline_comment_rows(self, MockClient) -> None:
        svc = PRSyncService()
        gh = MockClient.return_value

        gh.get_pr_bundle.return_value = _bundle_response()
        gh.get_timeline_page_back.return_value = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "timelineItems": {
                            "pageInfo": {"hasPreviousPage": False, "startCursor": "CUR-1"},
                            "nodes": [
                                _review_node(
                                    review_id="REV_BACK_1",
                                    submitted_at="2024-01-10T00:00:00Z",
                                    state="COMMENTED",
                                    inline_comments=[
                                        _inline_comment(node_id="IC_BACK_1"),
                                        _inline_comment(node_id="IC_BACK_2", line=42),
                                    ],
                                ),
                            ],
                        }
                    }
                }
            }
        }

        with mock.patch.object(PRSyncService, "sync_pull_request_bundle", return_value=dict(_BUNDLE_NOOP_RESULT)):
            res = svc.sync_pull_request(
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
        review_event = PRTimelineEvent.objects.get(pull_request=pr, github_node_id="REV_BACK_1")
        self.assertEqual(review_event.type, PRTimelineEventType.REVIEW_COMMENTED)
        self.assertEqual(review_event.inline_comment_total_count, 2)

        # The two inline comments persisted with FK to the parent event.
        rows = PRReviewInlineComment.objects.filter(pull_request=pr, review_node_id="REV_BACK_1").order_by("github_node_id")
        self.assertEqual(rows.count(), 2)
        for row in rows:
            self.assertEqual(row.parent_review_event_id, review_event.pk)
        self.assertEqual(res.get("inline_comments_created"), 2)

    @mock.patch("syncer.services.pr_sync_service.GitHubClient")
    def test_back_page_creates_backfill_marker_when_more_pages(self, MockClient) -> None:
        svc = PRSyncService()
        gh = MockClient.return_value

        gh.get_pr_bundle.return_value = _bundle_response()
        gh.get_timeline_page_back.return_value = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "timelineItems": {
                            "pageInfo": {"hasPreviousPage": False, "startCursor": "CUR-1"},
                            "nodes": [
                                _review_node(
                                    review_id="REV_BIG",
                                    submitted_at="2024-01-10T00:00:00Z",
                                    state="APPROVED",
                                    inline_comments=[_inline_comment(node_id="IC_BIG_1")],
                                    has_next_page=True,
                                    total_count=42,
                                ),
                            ],
                        }
                    }
                }
            }
        }

        with mock.patch.object(PRSyncService, "sync_pull_request_bundle", return_value=dict(_BUNDLE_NOOP_RESULT)):
            res = svc.sync_pull_request(
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
        review_event = PRTimelineEvent.objects.get(pull_request=pr, github_node_id="REV_BIG")
        # A backfill marker was created — this is the v3-recovery hook.
        backfill = PRReviewInlineCommentBackfill.objects.get(review_event=review_event)
        self.assertEqual(backfill.review_node_id, "REV_BIG")
        self.assertEqual(backfill.total_count, 42)
        self.assertEqual(res.get("inline_backfill_rows_upserted"), 1)

    @mock.patch("syncer.services.pr_sync_service.GitHubClient")
    def test_back_page_with_no_review_nodes_is_noop(self, MockClient) -> None:
        svc = PRSyncService()
        gh = MockClient.return_value

        gh.get_pr_bundle.return_value = _bundle_response()
        gh.get_timeline_page_back.return_value = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "timelineItems": {
                            "pageInfo": {"hasPreviousPage": False, "startCursor": "CUR-1"},
                            "nodes": [
                                {
                                    "__typename": "LabeledEvent",
                                    "id": "e1",
                                    "createdAt": "2024-01-10T00:00:00Z",
                                    "label": {"name": "X"},
                                },
                            ],
                        }
                    }
                }
            }
        }

        with mock.patch.object(PRSyncService, "sync_pull_request_bundle", return_value=dict(_BUNDLE_NOOP_RESULT)):
            res = svc.sync_pull_request(
                self.repo,
                number=99,
                client=gh,
                timelineK=2,
                commitsM=0,
                max_timeline_pages=0,
                max_commit_pages=0,
                backfill_timeline_pages=1,
            )

        # Label event was persisted, no inline comments were created.
        self.assertEqual(PRReviewInlineComment.objects.count(), 0)
        self.assertEqual(PRReviewInlineCommentBackfill.objects.count(), 0)
        self.assertEqual(res.get("inline_comments_created"), 0)
        self.assertEqual(res.get("inline_backfill_rows_upserted"), 0)


class TestForwardPagePersistsInlineComments(TestCase):
    """Forward (`get_timeline_page`) page loop must persist inline comments.

    Less critical than the back path (the wave triggers back-pages, not
    forward-pages), but the same invariant: any code path that processes
    timeline nodes must invoke the inline-comments sub-sync.
    """

    def setUp(self) -> None:
        self.repo = make_repo()
        make_pr(self.repo, 100)

    @mock.patch("syncer.services.pr_sync_service.GitHubClient")
    def test_forward_page_creates_inline_comment_rows(self, MockClient) -> None:
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
                        "timelineItems": {
                            "pageInfo": {
                                "hasNextPage": True,
                                "endCursor": "CUR_FWD",
                                "hasPreviousPage": False,
                                "startCursor": None,
                            },
                            "nodes": [],
                        },
                        "commits": {"pageInfo": {"hasPreviousPage": False, "startCursor": None}, "nodes": []},
                    },
                }
            }
        }
        gh.get_timeline_page.return_value = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "timelineItems": {
                            "pageInfo": {"hasNextPage": False, "endCursor": "CUR_FWD2"},
                            "nodes": [
                                _review_node(
                                    review_id="REV_FWD_1",
                                    submitted_at="2024-02-10T00:00:00Z",
                                    state="CHANGES_REQUESTED",
                                    inline_comments=[
                                        _inline_comment(node_id="IC_FWD_1", path="src/bar.py"),
                                    ],
                                ),
                            ],
                        }
                    }
                }
            }
        }

        with mock.patch.object(PRSyncService, "sync_pull_request_bundle", return_value=dict(_BUNDLE_NOOP_RESULT)):
            res = svc.sync_pull_request(
                self.repo,
                number=100,
                client=gh,
                timelineK=2,
                commitsM=0,
                max_timeline_pages=1,
                max_commit_pages=0,
                backfill_timeline_pages=0,
            )

        pr = PullRequest.objects.get(repository=self.repo, number=100)
        review_event = PRTimelineEvent.objects.get(pull_request=pr, github_node_id="REV_FWD_1")
        self.assertEqual(review_event.type, PRTimelineEventType.REVIEW_CHANGES_REQUESTED)
        rows = PRReviewInlineComment.objects.filter(pull_request=pr, review_node_id="REV_FWD_1")
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().parent_review_event_id, review_event.pk)
        self.assertEqual(res.get("inline_comments_created"), 1)
