from __future__ import annotations

from django.test import TestCase

from core.models.repository import Repository
from core.models import User
from syncer.services.sub.pull_request_sync import upsert_pull_request
from syncer.models import PullRequest
from syncer.tests.factories import make_repo


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
