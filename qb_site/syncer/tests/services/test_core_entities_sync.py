from __future__ import annotations

from django.test import TestCase

from core.models.repository import Repository
from core.models import User
from syncer.services.sub.core_entities_sync import upsert_repo_node_id, upsert_user_from_github
from syncer.tests.factories import make_repo


class TestCoreEntitiesSync(TestCase):
    def setUp(self) -> None:
        self.repo = make_repo()

    def test_upsert_repo_node_id(self) -> None:
        changed = upsert_repo_node_id(self.repo, None)
        self.assertFalse(changed)
        changed = upsert_repo_node_id(self.repo, "R_repo1")
        self.assertTrue(changed)
        self.repo.refresh_from_db()
        self.assertEqual(self.repo.github_node_id, "R_repo1")
        # Idempotent
        changed = upsert_repo_node_id(self.repo, "R_repo1")
        self.assertFalse(changed)

    def test_upsert_user_from_github_create_and_update(self) -> None:
        # Create by node id + login
        actor = {"__typename": "User", "id": "U1", "login": "alice", "name": "Alice", "avatarUrl": "https://ex/a.png"}
        user, created, updated = upsert_user_from_github(actor)
        self.assertIsNotNone(user)
        assert user is not None
        self.assertTrue(created)
        self.assertEqual(user.github_node_id, "U1")
        self.assertEqual(user.github_login, "alice")
        self.assertEqual(user.name, "Alice")
        self.assertTrue(user.avatar_url.endswith("a.png"))

        # Update name/avatar only
        actor["name"] = "Alice A."
        actor["avatarUrl"] = "https://ex/b.png"
        user2, created2, updated2 = upsert_user_from_github(actor)
        self.assertFalse(created2)
        self.assertIn("name", updated2)
        self.assertIn("avatar_url", updated2)
        self.assertEqual(user2.id, user.id)

    def test_upsert_user_by_login_then_backfill_node_id(self) -> None:
        # Create by login only
        actor = {"__typename": "User", "login": "bob"}
        user, created, updated = upsert_user_from_github(actor)
        self.assertTrue(created)
        assert user is not None
        self.assertIsNone(user.github_node_id)
        # Later provide node id; ensure it fills in
        actor2 = {"__typename": "User", "id": "U2", "login": "bob"}
        user2, created2, updated2 = upsert_user_from_github(actor2)
        self.assertFalse(created2)
        self.assertIn("github_node_id", updated2)
        self.assertEqual(user2.github_node_id, "U2")
