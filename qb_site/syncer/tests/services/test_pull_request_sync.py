from __future__ import annotations

from django.test import TestCase
from django.utils import timezone

from core.models import User
from syncer.services.sub.pull_request_sync import upsert_pull_request
from syncer.services.pr_sync_service import PRSyncService
from syncer.models import PullRequest
from syncer.tests.factories import make_repo, make_pr


class TestPullRequestSync(TestCase):
    def setUp(self) -> None:
        self.repo = make_repo()

    def test_create_and_update(self) -> None:
        bundle = {
            "number": 1,
            "state": "OPEN",
            "isDraft": False,
            "title": "First",
            "body": "body",
            "createdAt": "2025-10-20T10:00:00Z",
            "updatedAt": "2025-10-20T10:05:00Z",
            "baseRefName": "master",
            "headRefName": "b",
            "headRefOid": "abc123",
            "headRepositoryOwner": {"login": "o"},
            "headRepository": {"name": "fork"},
            "additions": 1,
            "deletions": 0,
            "changedFiles": 1,
            "author": {"login": "Alice"},
        }
        res = upsert_pull_request(bundle, self.repo)
        self.assertTrue(res.created)
        pr = PullRequest.objects.get(repository=self.repo, number=1)
        self.assertEqual(pr.title, "First")
        self.assertEqual(pr.head_sha, "abc123")
        self.assertIsNotNone(pr.gh_created_at)
        # Author created case-insensitively
        self.assertTrue(User.objects.filter(github_login__iexact="alice").exists())

        # Update
        bundle["title"] = "Second"
        bundle["headRefOid"] = "def456"
        res2 = upsert_pull_request(bundle, self.repo)
        self.assertFalse(res2.created)
        self.assertIn("title", res2.updated_fields)
        self.assertIn("head_sha", res2.updated_fields)
        pr.refresh_from_db()
        self.assertEqual(pr.title, "Second")
        self.assertEqual(pr.head_sha, "def456")

    def test_upsert_does_not_advance_last_synced_at_for_existing_pr(self) -> None:
        """upsert_pull_request must not advance last_synced_at for existing PRs.

        last_synced_at is used by the sync skip check: if it were advanced here
        (before engagement fields like assignees are saved), a task failure between
        the two saves would leave the PR with stale assignees permanently — all
        retries would see gh_updated <= last_synced_at and skip the full bundle.
        last_synced_at is now advanced only inside sync_pull_request_bundle, after
        engagement fields are also written.
        """
        bundle = {
            "number": 10,
            "state": "OPEN",
            "isDraft": False,
            "title": "Original title",
            "body": "",
            "createdAt": "2025-01-01T00:00:00Z",
            "updatedAt": "2025-01-01T01:00:00Z",
            "baseRefName": "master",
            "headRefName": "branch",
            "headRefOid": "aaa111",
            "headRepositoryOwner": {"login": "org"},
            "headRepository": {"name": "repo"},
            "additions": 0,
            "deletions": 0,
            "changedFiles": 0,
            "author": {"login": "alice"},
        }
        upsert_pull_request(bundle, self.repo)
        pr = PullRequest.objects.get(repository=self.repo, number=10)
        original_last_synced_at = pr.last_synced_at

        bundle["title"] = "Updated title"
        upsert_pull_request(bundle, self.repo)
        pr.refresh_from_db()

        self.assertEqual(pr.last_synced_at, original_last_synced_at)
        self.assertEqual(pr.title, "Updated title")

    def test_sync_pull_request_bundle_advances_last_synced_at(self) -> None:
        """sync_pull_request_bundle must advance last_synced_at after saving engagement fields."""
        t_before = timezone.now()
        pr = make_pr(self.repo, 20)
        pr.last_synced_at = t_before
        pr.save(update_fields=["last_synced_at"])

        bundle = {
            "number": 20,
            "state": "OPEN",
            "isDraft": False,
            "title": pr.title,
            "body": "",
            "createdAt": "2025-01-01T00:00:00Z",
            "updatedAt": "2025-01-01T01:00:00Z",
            "baseRefName": "master",
            "headRefName": "branch",
            "headRefOid": "bbb222",
            "headRepositoryOwner": {"login": "org"},
            "headRepository": {"name": "repo"},
            "additions": 0,
            "deletions": 0,
            "changedFiles": 0,
            "author": {"login": "alice"},
            "labels": {"nodes": []},
            "timelineItems": {"nodes": []},
            "commits": {"nodes": []},
            "files": {"nodes": [], "pageInfo": {"hasNextPage": False}, "totalCount": 0},
            "assignees": {
                "nodes": [{"login": "bob"}],
                "pageInfo": {"hasNextPage": False},
                "totalCount": 1,
            },
            "reviews": {"nodes": [], "pageInfo": {"hasNextPage": False}, "totalCount": 0},
            "comments": {"nodes": [], "pageInfo": {"hasNextPage": False}, "totalCount": 0},
            "reviewThreads": {"nodes": [], "pageInfo": {"hasNextPage": False}, "totalCount": 0},
        }
        PRSyncService().sync_pull_request_bundle(self.repo, bundle)
        pr.refresh_from_db()

        self.assertGreater(pr.last_synced_at, t_before)
        self.assertEqual(pr.assignees, ["bob"])

    def test_author_upsert_with_node_id_and_metadata(self) -> None:
        bundle = {
            "number": 2,
            "state": "OPEN",
            "isDraft": False,
            "title": "With Author",
            "body": "",
            "createdAt": "2025-10-20T10:00:00Z",
            "updatedAt": "2025-10-20T10:05:00Z",
            "baseRefName": "master",
            "headRefName": "b",
            "headRepositoryOwner": {"login": "o"},
            "headRepository": {"name": "fork"},
            "additions": 0,
            "deletions": 0,
            "changedFiles": 0,
            "author": {"id": "UID123", "login": "alice", "name": "Alice A.", "avatarUrl": "https://ex/a.png"},
        }
        res = upsert_pull_request(bundle, self.repo)
        self.assertTrue(res.created)
        pr = PullRequest.objects.get(repository=self.repo, number=2)
        self.assertIsNotNone(pr.author)
        self.assertEqual(pr.author.github_node_id, "UID123")
        self.assertEqual(pr.author.github_login, "alice")
        self.assertEqual(pr.author.name, "Alice A.")
        self.assertTrue(pr.author.avatar_url.endswith("a.png"))
