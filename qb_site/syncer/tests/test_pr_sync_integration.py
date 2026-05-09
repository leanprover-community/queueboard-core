from __future__ import annotations

from django.test import TestCase
from django.utils import timezone
from django.conf import settings
from datetime import timezone as dt_timezone

from core.models.repository import Repository
from syncer.models import (
    PullRequest,
    LabelDef,
    PRLabel,
    PRTimelineEvent,
    CommitCheckRun,
    CommitStatusContext,
)
from syncer.services.pr_sync_service import PRSyncService
import json
from pathlib import Path


class TestPRSyncIntegration(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="o", name="r", default_branch="master", is_active=True)

    def _make_min_bundle(self) -> dict:
        # Minimal pullRequest-shaped bundle for sync_pull_request_bundle
        return {
            "number": 1,
            "state": "OPEN",
            "isDraft": False,
            "title": "Baseline",
            "body": "",
            "createdAt": "2025-10-20T00:00:00Z",
            "updatedAt": "2025-10-20T00:10:00Z",
            "closedAt": None,
            "mergedAt": None,
            "baseRefName": "master",
            "headRefName": "feature",
            "headRepositoryOwner": {"login": "o"},
            "headRepository": {"name": "fork"},
            "additions": 2,
            "deletions": 1,
            "changedFiles": 1,
            "author": {"login": "alice"},
            "labels": {
                "nodes": [
                    {"name": "A", "color": "ffffff"},
                    {"name": "B", "color": "000000"},
                ]
            },
            "timelineItems": {
                "nodes": [
                    {"__typename": "LabeledEvent", "id": "E1", "createdAt": "2025-10-20T00:02:00Z", "label": {"name": "A"}},
                    {"__typename": "ReadyForReviewEvent", "id": "E2", "createdAt": "2025-10-20T00:03:00Z"},
                    {"__typename": "ClosedEvent", "id": "E3", "createdAt": "2025-10-20T00:04:00Z"},
                ]
            },
            "commits": {
                "nodes": [
                    {
                        "commit": {
                            "committedDate": "2025-10-20T00:05:00Z",
                            "oid": "abc123abc123abc123abc123abc123abc123abcd",
                            "statusCheckRollup": {
                                "contexts": {
                                    "nodes": [
                                        {
                                            "__typename": "CheckRun",
                                            "id": "CR1",
                                            "name": "build",
                                            "status": "COMPLETED",
                                            "conclusion": "SUCCESS",
                                            "startedAt": "2025-10-20T00:05:00Z",
                                            "completedAt": "2025-10-20T00:06:00Z",
                                            "detailsUrl": None,
                                            "externalId": None,
                                        },
                                        {
                                            "__typename": "StatusContext",
                                            "id": "SC1",
                                            "context": "bors",
                                            "state": "SUCCESS",
                                            "targetUrl": None,
                                            "description": "",
                                            "createdAt": "2025-10-20T00:06:30Z",
                                        },
                                    ]
                                }
                            },
                        }
                    }
                ]
            },
        }

    def test_baseline_bundle_ingest(self) -> None:
        svc = PRSyncService()
        bundle = self._make_min_bundle()

        res = svc.sync_pull_request_bundle(self.repo, bundle, dry_run=False)

        # PullRequest created
        pr = PullRequest.objects.get(repository=self.repo, number=1)
        self.assertEqual(pr.title, "Baseline")
        self.assertIsNotNone(pr.gh_created_at)

        # Label catalog + attachments created
        self.assertEqual(LabelDef.objects.filter(repository=self.repo).count(), 2)
        self.assertEqual(PRLabel.objects.filter(pull_request=pr).count(), 2)

        # Timeline events created (3 nodes)
        self.assertEqual(PRTimelineEvent.objects.filter(pull_request=pr).count(), 3)

        # CI snapshots created in SHA-keyed storage
        self.assertEqual(CommitCheckRun.objects.filter(repository=self.repo).count(), 1)
        self.assertEqual(CommitStatusContext.objects.filter(repository=self.repo).count(), 1)

        # Return dict includes created counts consistent with rows
        self.assertEqual(res["prlabels_created"], 2)
        self.assertEqual(res["events_created"], 3)
        self.assertEqual(res["checkruns_upserted"], 1)
        self.assertEqual(res["statusctx_upserted"], 1)

    def test_marks_head_ci_unavailable_for_stale_missing_rollup(self) -> None:
        svc = PRSyncService()
        bundle = self._make_min_bundle()
        bundle["createdAt"] = "2020-01-01T00:00:00Z"
        bundle["updatedAt"] = "2020-01-01T00:10:00Z"
        bundle["commits"]["nodes"][0]["commit"]["statusCheckRollup"] = None
        bundle["commits"]["nodes"][0]["commit"]["committedDate"] = "2020-01-01T00:05:00Z"

        svc.sync_pull_request_bundle(self.repo, bundle, dry_run=False)

        pr = PullRequest.objects.get(repository=self.repo, number=1)
        self.assertEqual(pr.head_ci_state, "UNAVAILABLE")

    def test_skips_unavailable_for_recent_missing_rollup(self) -> None:
        svc = PRSyncService()
        bundle = self._make_min_bundle()
        recent = timezone.now() - timezone.timedelta(days=30)
        iso_recent = recent.isoformat()
        bundle["createdAt"] = iso_recent
        bundle["updatedAt"] = iso_recent
        bundle["commits"]["nodes"][0]["commit"]["statusCheckRollup"] = None
        bundle["commits"]["nodes"][0]["commit"]["committedDate"] = iso_recent

        svc.sync_pull_request_bundle(self.repo, bundle, dry_run=False)

        pr = PullRequest.objects.get(repository=self.repo, number=1)
        self.assertIsNone(pr.head_ci_state)

    def test_idempotent_reingest(self) -> None:
        svc = PRSyncService()
        bundle = self._make_min_bundle()

        # First run creates rows
        svc.sync_pull_request_bundle(self.repo, bundle, dry_run=False)
        pr = PullRequest.objects.get(repository=self.repo, number=1)
        self.assertEqual(PRLabel.objects.filter(pull_request=pr).count(), 2)
        self.assertEqual(PRTimelineEvent.objects.filter(pull_request=pr).count(), 3)
        self.assertEqual(CommitCheckRun.objects.filter(repository=self.repo).count(), 1)
        self.assertEqual(CommitStatusContext.objects.filter(repository=self.repo).count(), 1)

        # Second run is idempotent
        res2 = svc.sync_pull_request_bundle(self.repo, bundle, dry_run=False)
        self.assertEqual(res2["labels_created"], 0)
        self.assertEqual(res2["labels_updated"], 0)
        self.assertEqual(res2["prlabels_created"], 0)
        self.assertEqual(res2["prlabels_deleted"], 0)
        self.assertEqual(res2["events_created"], 0)
        self.assertEqual(res2["checkruns_upserted"], 0)
        self.assertEqual(res2["statusctx_upserted"], 0)

        # DB counts unchanged
        self.assertEqual(PRLabel.objects.filter(pull_request=pr).count(), 2)
        self.assertEqual(PRTimelineEvent.objects.filter(pull_request=pr).count(), 3)
        self.assertEqual(CommitCheckRun.objects.filter(repository=self.repo).count(), 1)
        self.assertEqual(CommitStatusContext.objects.filter(repository=self.repo).count(), 1)

    def test_engagement_fields_ingest(self) -> None:
        svc = PRSyncService()
        bundle = self._make_min_bundle()
        bundle["changedFiles"] = 2
        bundle["files"] = {
            "pageInfo": {"hasNextPage": False},
            "nodes": [
                {"path": "src/a.lean", "changeType": "MODIFIED", "additions": 2, "deletions": 1},
                {"path": "src/b.lean", "changeType": "ADDED", "additions": 5, "deletions": 0},
            ],
        }
        bundle["assignees"] = {
            "totalCount": 2,
            "pageInfo": {"hasNextPage": False},
            "nodes": [
                {"id": "U1", "login": "assignee1", "name": "Assignee One", "avatarUrl": "https://ex/a.png"},
                {"id": "U2", "login": "assignee2", "name": "Assignee Two", "avatarUrl": "https://ex/b.png"},
            ],
        }
        bundle["reviews"] = {
            "totalCount": 2,
            "pageInfo": {"hasNextPage": False},
            "nodes": [
                {
                    "id": "R1",
                    "state": "APPROVED",
                    "submittedAt": "2025-10-20T00:07:00Z",
                    "author": {
                        "__typename": "User",
                        "id": "A1",
                        "login": "rev1",
                        "name": "Reviewer 1",
                        "avatarUrl": "https://ex/r1.png",
                    },
                },
                {
                    "id": "R2",
                    "state": "COMMENTED",
                    "submittedAt": "2025-10-20T00:08:00Z",
                    "author": {
                        "__typename": "User",
                        "id": "A2",
                        "login": "rev2",
                        "name": "Reviewer 2",
                        "avatarUrl": "https://ex/r2.png",
                    },
                },
            ],
        }
        bundle["comments"] = {
            "totalCount": 2,
            "pageInfo": {"hasNextPage": False},
            "nodes": [
                {
                    "id": "C1",
                    "createdAt": "2025-10-20T00:09:00Z",
                    "author": {
                        "__typename": "User",
                        "id": "C1",
                        "login": "commenter1",
                        "name": "Commenter 1",
                        "avatarUrl": "https://ex/c1.png",
                    },
                },
                {
                    "id": "C2",
                    "createdAt": "2025-10-20T00:10:00Z",
                    "author": {
                        "__typename": "User",
                        "id": "C2",
                        "login": "rev2",
                        "name": "Reviewer 2",
                        "avatarUrl": "https://ex/r2.png",
                    },
                },
            ],
        }
        bundle["reviewThreads"] = {
            "totalCount": 1,
            "pageInfo": {"hasNextPage": False},
            "nodes": [
                {
                    "comments": {
                        "totalCount": 5,
                    }
                }
            ],
        }

        svc.sync_pull_request_bundle(self.repo, bundle, dry_run=False)
        pr = PullRequest.objects.get(repository=self.repo, number=1)

        self.assertEqual(pr.files, ["src/a.lean", "src/b.lean"])
        self.assertFalse(pr.files_incomplete)
        self.assertEqual(pr.assignees, ["assignee1", "assignee2"])
        self.assertFalse(pr.assignees_incomplete)
        self.assertEqual(pr.approvals, ["rev1"])
        self.assertFalse(pr.reviews_incomplete)
        self.assertEqual(pr.commenters, ["commenter1", "rev1", "rev2"])
        self.assertEqual(pr.number_total_comments, 7)
        self.assertFalse(pr.comments_incomplete)
        self.assertIsNotNone(pr.last_synced_at)

    def test_labels_diff_attach_and_detach(self) -> None:
        svc = PRSyncService()
        bundle = self._make_min_bundle()  # A, B
        svc.sync_pull_request_bundle(self.repo, bundle, dry_run=False)
        pr = PullRequest.objects.get(repository=self.repo, number=1)
        self.assertEqual(PRLabel.objects.filter(pull_request=pr).count(), 2)

        # Change labels to A, C and re-ingest
        bundle2 = self._make_min_bundle()
        bundle2["labels"]["nodes"] = [
            {"name": "A", "color": "ffffff"},
            {"name": "C", "color": "123456"},
        ]
        res = svc.sync_pull_request_bundle(self.repo, bundle2, dry_run=False)

        # LabelDef: C created (catalog grows)
        self.assertGreaterEqual(LabelDef.objects.filter(repository=self.repo).count(), 3)

        # Attach C (+1), detach B (-1)
        self.assertEqual(res["labels_created"], 1)
        self.assertEqual(res["labels_updated"], 0)
        self.assertEqual(res["prlabels_created"], 1)
        self.assertEqual(res["prlabels_deleted"], 1)

        # Still 2 attachments total
        self.assertEqual(PRLabel.objects.filter(pull_request=pr).count(), 2)

    def test_timeline_since_and_paging(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.bundle_calls: list[dict] = []
                self.timeline_page_calls: list[dict] = []

            def get_pr_bundle(self, *, owner, name, number, timelineK, commitsM, timeline_since_iso=None, query_path=None):  # type: ignore[no-redef]
                # Record since for assertion
                self.bundle_calls.append(
                    {
                        "owner": owner,
                        "name": name,
                        "number": number,
                        "timelineK": timelineK,
                        "commitsM": commitsM,
                        "since": timeline_since_iso,
                    }
                )
                return {
                    "data": {
                        "repository": {
                            "id": "R1",
                            "name": name,
                            "owner": {"login": owner},
                            "defaultBranchRef": {"name": "master"},
                            "pullRequest": {
                                "number": number,
                                "state": "OPEN",
                                "isDraft": False,
                                "title": "T",
                                "body": "",
                                "createdAt": "2025-10-20T00:00:00Z",
                                "updatedAt": "2025-10-20T00:10:00Z",
                                "baseRefName": "master",
                                "headRefName": "b",
                                "headRepositoryOwner": {"login": owner},
                                "headRepository": {"name": name},
                                "additions": 0,
                                "deletions": 0,
                                "changedFiles": 0,
                                "author": {"login": "alice"},
                                "labels": {"nodes": []},
                                "timelineItems": {
                                    "pageInfo": {"hasNextPage": True, "endCursor": "t1"},
                                    "nodes": [
                                        {
                                            "__typename": "LabeledEvent",
                                            "id": "TE1",
                                            "createdAt": "2025-10-20T00:06:00Z",
                                            "label": {"name": "A"},
                                        }
                                    ],
                                },
                                "commits": {"pageInfo": {"hasPreviousPage": False, "startCursor": None}, "nodes": []},
                            },
                        }
                    }
                }

            def get_timeline_page(self, *, owner, name, number, first, after, since_iso=None, query_path=None):  # type: ignore[no-redef]
                self.timeline_page_calls.append(
                    {"owner": owner, "name": name, "number": number, "first": first, "after": after, "since": since_iso}
                )
                return {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "timelineItems": {
                                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                                    "nodes": [{"__typename": "ClosedEvent", "id": "TE2", "createdAt": "2025-10-20T00:07:00Z"}],
                                }
                            }
                        }
                    }
                }

            def get_last_rate_limit(self):  # not used when rate_log=None
                return None

        # Existing PR with last_synced_at so service computes timeline_since_iso
        pr = PullRequest.objects.create(
            repository=self.repo,
            number=5,
            state="open",
            is_draft=False,
            gh_created_at=timezone.now(),
            gh_updated_at=timezone.now(),
            base_ref_name="master",
            head_ref_name="b",
            head_repo_owner_login="o",
            head_repo_name="r",
            title="t",
            body="",
            additions=0,
            deletions=0,
            changed_files_count=0,
            last_synced_at=timezone.make_aware(timezone.datetime(2025, 10, 20, 0, 5, 0)),
        )

        svc = PRSyncService()
        fc = FakeClient()
        svc.sync_pull_request(
            self.repo,
            number=5,
            client=fc,  # type: ignore[arg-type]
            timelineK=2,
            commitsM=1,
            max_timeline_pages=1,
            dry_run=False,
        )

        # Assert since was passed to bundle and paging with expected epsilon
        self.assertTrue(fc.bundle_calls)
        b = fc.bundle_calls[0]
        eps = int(getattr(settings, "SYNCER_LAST_SYNC_EPSILON_SECONDS", 2))
        expected_since = (
            (pr.last_synced_at - timezone.timedelta(seconds=eps)).astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        self.assertEqual(b["since"], expected_since)
        self.assertTrue(fc.timeline_page_calls)
        t = fc.timeline_page_calls[0]
        self.assertEqual(t["after"], "t1")
        self.assertEqual(t["since"], expected_since)

        # Two timeline events total ingested (bundle + page)
        self.assertEqual(PRTimelineEvent.objects.filter(pull_request=pr).count(), 2)

    def test_commits_capped_paging(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.commits_page_calls: list[dict] = []

            def get_pr_bundle(self, *, owner, name, number, timelineK, commitsM, timeline_since_iso=None, query_path=None):  # type: ignore[no-redef]
                return {
                    "data": {
                        "repository": {
                            "id": "R1",
                            "name": name,
                            "owner": {"login": owner},
                            "defaultBranchRef": {"name": "master"},
                            "pullRequest": {
                                "number": number,
                                "state": "OPEN",
                                "isDraft": False,
                                "title": "T",
                                "body": "",
                                "createdAt": "2025-10-20T00:00:00Z",
                                "updatedAt": "2025-10-20T00:10:00Z",
                                "baseRefName": "master",
                                "headRefName": "b",
                                "headRepositoryOwner": {"login": owner},
                                "headRepository": {"name": name},
                                "additions": 0,
                                "deletions": 0,
                                "changedFiles": 0,
                                "author": {"login": "alice"},
                                "labels": {"nodes": []},
                                "timelineItems": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []},
                                "commits": {
                                    "pageInfo": {"hasPreviousPage": True, "startCursor": "c1"},
                                    "nodes": [
                                        {
                                            "commit": {
                                                "oid": "h1",
                                                "statusCheckRollup": {
                                                    "contexts": {
                                                        "nodes": [
                                                            {
                                                                "__typename": "CheckRun",
                                                                "id": "CRx",
                                                                "name": "build",
                                                                "status": "COMPLETED",
                                                                "conclusion": "SUCCESS",
                                                                "startedAt": "2025-10-20T00:05:00Z",
                                                                "completedAt": "2025-10-20T00:06:00Z",
                                                            }
                                                        ]
                                                    }
                                                },
                                            }
                                        }
                                    ],
                                },
                            },
                        }
                    }
                }

            def get_timeline_page(self, **kwargs):  # not used in this test
                return {"data": {"repository": {"pullRequest": {"timelineItems": {"pageInfo": {"hasNextPage": False}}}}}}

            def get_commits_page(self, *, owner, name, number, last, before, query_path=None):  # type: ignore[no-redef]
                self.commits_page_calls.append({"before": before, "last": last, "owner": owner, "name": name, "number": number})
                return {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "commits": {
                                    "pageInfo": {"hasPreviousPage": False, "startCursor": None},
                                    "nodes": [
                                        {
                                            "commit": {
                                                "oid": "h0",
                                                "statusCheckRollup": {
                                                    "contexts": {
                                                        "nodes": [
                                                            {
                                                                "__typename": "StatusContext",
                                                                "id": "SCy",
                                                                "context": "bors",
                                                                "state": "SUCCESS",
                                                                "createdAt": "2025-10-20T00:04:00Z",
                                                            }
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

            def get_last_rate_limit(self):
                return None

        svc = PRSyncService()
        fc = FakeClient()
        svc.sync_pull_request(
            self.repo,
            number=7,
            client=fc,  # type: ignore[arg-type]
            timelineK=2,
            commitsM=1,
            max_commit_pages=1,
            dry_run=False,
        )

        # We fetched one extra commits page and upserted both CR (bundle) and SC (page)
        self.assertTrue(fc.commits_page_calls)
        self.assertEqual(CommitCheckRun.objects.filter(repository=self.repo).count(), 1)
        self.assertEqual(CommitStatusContext.objects.filter(repository=self.repo).count(), 1)

    def test_force_push_event_ingest(self) -> None:
        svc = PRSyncService()
        bundle = {
            "number": 2,
            "state": "OPEN",
            "isDraft": False,
            "title": "ForcePush",
            "body": "",
            "createdAt": "2025-10-20T00:00:00Z",
            "updatedAt": "2025-10-20T00:10:00Z",
            "baseRefName": "master",
            "headRefName": "b",
            "headRepositoryOwner": {"login": "o"},
            "headRepository": {"name": "r"},
            "additions": 0,
            "deletions": 0,
            "changedFiles": 0,
            "author": {"login": "alice"},
            "labels": {"nodes": []},
            "timelineItems": {
                "nodes": [
                    {
                        "__typename": "HeadRefForcePushedEvent",
                        "id": "FP1",
                        "createdAt": "2025-10-20T00:06:00Z",
                        "beforeCommit": {"oid": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
                        "afterCommit": {"oid": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},
                    }
                ]
            },
            "commits": {"nodes": []},
        }

        svc.sync_pull_request_bundle(self.repo, bundle, dry_run=False)
        pr = PullRequest.objects.get(repository=self.repo, number=2)
        events = PRTimelineEvent.objects.filter(pull_request=pr)
        self.assertEqual(events.count(), 1)
        ev = events.first()
        assert ev is not None
        self.assertEqual(ev.type, "HEAD_FORCE_PUSHED")
        self.assertEqual(ev.before_sha, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        self.assertEqual(ev.after_sha, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")

    def test_ci_evolution_across_syncs(self) -> None:
        svc = PRSyncService()
        # First run: pending/in-progress
        bundle1 = {
            "number": 3,
            "state": "OPEN",
            "isDraft": False,
            "title": "CI Evolution",
            "body": "",
            "createdAt": "2025-10-20T00:00:00Z",
            "updatedAt": "2025-10-20T00:05:00Z",
            "baseRefName": "master",
            "headRefName": "b",
            "headRepositoryOwner": {"login": "o"},
            "headRepository": {"name": "r"},
            "additions": 0,
            "deletions": 0,
            "changedFiles": 0,
            "author": {"login": "alice"},
            "labels": {"nodes": []},
            "timelineItems": {"nodes": []},
            "commits": {
                "nodes": [
                    {
                        "commit": {
                            "oid": "h1",
                            "statusCheckRollup": {
                                "contexts": {
                                    "nodes": [
                                        {
                                            "__typename": "CheckRun",
                                            "id": "CR2",
                                            "name": "build",
                                            "status": "IN_PROGRESS",
                                            "conclusion": None,
                                            "startedAt": "2025-10-20T00:01:00Z",
                                            "completedAt": None,
                                        },
                                        {
                                            "__typename": "StatusContext",
                                            "id": "SC2",
                                            "context": "bors",
                                            "state": "PENDING",
                                            "createdAt": "2025-10-20T00:01:30Z",
                                        },
                                    ]
                                }
                            },
                        }
                    }
                ]
            },
        }
        res1 = svc.sync_pull_request_bundle(self.repo, bundle1, dry_run=False)
        self.assertEqual(CommitCheckRun.objects.filter(repository=self.repo, head_sha="h1").count(), 1)
        self.assertEqual(CommitStatusContext.objects.filter(repository=self.repo, head_sha="h1").count(), 1)
        self.assertEqual(res1["checkruns_upserted"], 1)
        self.assertEqual(res1["statusctx_upserted"], 1)

        # Second run: success/completed on same IDs
        bundle2 = {
            **bundle1,
            "updatedAt": "2025-10-20T00:10:00Z",
            "commits": {
                "nodes": [
                    {
                        "commit": {
                            "oid": "h1",
                            "statusCheckRollup": {
                                "contexts": {
                                    "nodes": [
                                        {
                                            "__typename": "CheckRun",
                                            "id": "CR2",
                                            "name": "build",
                                            "status": "COMPLETED",
                                            "conclusion": "SUCCESS",
                                            "startedAt": "2025-10-20T00:01:00Z",
                                            "completedAt": "2025-10-20T00:08:00Z",
                                        },
                                        {
                                            "__typename": "StatusContext",
                                            "id": "SC2",
                                            "context": "bors",
                                            "state": "SUCCESS",
                                            "createdAt": "2025-10-20T00:08:00Z",
                                        },
                                    ]
                                }
                            },
                        }
                    }
                ]
            },
        }
        res2 = svc.sync_pull_request_bundle(self.repo, bundle2, dry_run=False)
        self.assertEqual(res2["checkruns_upserted"], 1)  # updated
        self.assertEqual(res2["statusctx_upserted"], 1)  # updated
        # Verify DB reflects new states
        cr = CommitCheckRun.objects.get(repository=self.repo, github_node_id="CR2")
        self.assertEqual(cr.status, "COMPLETED")
        self.assertEqual(cr.conclusion, "SUCCESS")
        sc = CommitStatusContext.objects.get(repository=self.repo, github_node_id="SC2")
        self.assertEqual(sc.state, "SUCCESS")

    def test_real_bundle_timeline_paging_smoke(self) -> None:
        """Optional smoke test: page a real bundle's timeline with a small K.

        Skips if the optional fixture is missing or doesn't have enough supported events.
        """
        p = Path(__file__).resolve().parent / "fixtures" / "pr_bundle_real_forcepush.json"
        if not p.exists():
            self.skipTest("optional fixture pr_bundle_real_forcepush.json not present")
        data = json.loads(p.read_text())
        repo_node = (data.get("data") or {}).get("repository") or {}
        pr_node_full = repo_node.get("pullRequest") or {}
        if not pr_node_full:
            self.skipTest("fixture missing pullRequest node")

        # Count only event types we persist
        supported = {
            "LabeledEvent",
            "UnlabeledEvent",
            "ReadyForReviewEvent",
            "ConvertToDraftEvent",
            "ReopenedEvent",
            "ClosedEvent",
            "HeadRefForcePushedEvent",
        }
        all_nodes = (pr_node_full.get("timelineItems") or {}).get("nodes") or []
        supported_nodes = [n for n in all_nodes if isinstance(n, dict) and n.get("__typename") in supported]
        if len(supported_nodes) < 2:
            self.skipTest("fixture has fewer than 2 supported timeline nodes for paging")

        # Build a fake client that returns the first item in bundle and the rest on one page
        first = supported_nodes[:1]
        rest = supported_nodes[1:]

        class FakeClient:
            def __init__(self) -> None:
                self.timeline_page_calls: list[dict] = []

            def get_pr_bundle(self, *, owner, name, number, timelineK, commitsM, timeline_since_iso=None, query_path=None):  # type: ignore[no-redef]
                return {
                    "data": {
                        "repository": {
                            "id": repo_node.get("id"),
                            "name": name,
                            "owner": repo_node.get("owner") or {"login": owner},
                            "defaultBranchRef": repo_node.get("defaultBranchRef") or {"name": "master"},
                            "pullRequest": {
                                **{k: v for k, v in pr_node_full.items() if k not in {"timelineItems"}},
                                "timelineItems": {
                                    "pageInfo": {"hasNextPage": True, "endCursor": "tpage1"},
                                    "nodes": first,
                                },
                            },
                        }
                    }
                }

            def get_timeline_page(self, *, owner, name, number, first, after, since_iso=None, query_path=None):  # type: ignore[no-redef]
                self.timeline_page_calls.append({"after": after, "first": first})
                return {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "timelineItems": {
                                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                                    "nodes": rest,
                                }
                            }
                        }
                    }
                }

            def get_last_rate_limit(self):
                return None

        owner = (repo_node.get("owner") or {}).get("login") or "owner"
        name = repo_node.get("name") or "name"
        number = pr_node_full.get("number") or 0
        repo = Repository.objects.create(owner=owner, name=name, default_branch="master", is_active=True)

        svc = PRSyncService()
        fc = FakeClient()
        svc.sync_pull_request(
            repo,
            number=int(number),
            client=fc,  # type: ignore[arg-type]
            timelineK=1,
            commitsM=1,
            max_timeline_pages=2,
            dry_run=False,
        )

        pr = PullRequest.objects.get(repository=repo, number=int(number))
        db_count = PRTimelineEvent.objects.filter(pull_request=pr).count()
        self.assertEqual(db_count, len(supported_nodes))
        self.assertTrue(fc.timeline_page_calls)
