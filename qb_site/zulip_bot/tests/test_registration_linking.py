from __future__ import annotations

from django.test import TestCase

from core.models import User
from core.services.github_oauth import GitHubUserIdentity
from zulip_bot.services.registration_linking import (
    RegistrationLinkConflict,
    link_or_create_user_from_registration,
)


class TestRegistrationLinking(TestCase):
    def _identity(self, *, node_id: str = "U_node_1", login: str = "reviewer") -> GitHubUserIdentity:
        return GitHubUserIdentity(
            github_user_id=123,
            github_node_id=node_id,
            github_login=login,
            github_name="Reviewer User",
            github_avatar_url="https://example.com/avatar.png",
        )

    def test_create_new_user_when_no_match_exists(self) -> None:
        result = link_or_create_user_from_registration(
            zulip_user_id=101,
            zulip_full_name="Reviewer User",
            identity=self._identity(),
        )

        self.assertEqual(result.outcome, "created")
        self.assertEqual(User.objects.count(), 1)
        user = User.objects.get()
        self.assertEqual(user.github_node_id, "U_node_1")
        self.assertEqual(user.github_login, "reviewer")
        self.assertEqual(user.zulip_user_id, 101)
        self.assertEqual(user.zulip_full_name, "Reviewer User")

    def test_link_existing_user_by_github_node_id(self) -> None:
        user = User.objects.create(github_node_id="U_node_1", github_login="reviewer", zulip_user_id=None)

        result = link_or_create_user_from_registration(
            zulip_user_id=101,
            zulip_full_name="Reviewer User",
            identity=self._identity(),
        )

        self.assertEqual(result.outcome, "linked_existing")
        user.refresh_from_db()
        self.assertEqual(user.zulip_user_id, 101)
        self.assertEqual(user.zulip_full_name, "Reviewer User")

    def test_already_linked_user_is_idempotent(self) -> None:
        user = User.objects.create(github_node_id="U_node_1", github_login="old-casing", zulip_user_id=101)

        result = link_or_create_user_from_registration(
            zulip_user_id=101,
            zulip_full_name="Reviewer User",
            identity=self._identity(login="Reviewer"),
        )

        self.assertEqual(result.outcome, "already_linked")
        user.refresh_from_db()
        self.assertEqual(user.github_login, "Reviewer")

    def test_conflict_when_existing_user_is_linked_to_other_zulip(self) -> None:
        User.objects.create(github_node_id="U_node_1", github_login="reviewer", zulip_user_id=202)

        with self.assertRaises(RegistrationLinkConflict):
            link_or_create_user_from_registration(
                zulip_user_id=101,
                zulip_full_name="Reviewer User",
                identity=self._identity(),
            )

    def test_conflict_when_login_is_bound_to_different_node_id(self) -> None:
        User.objects.create(github_node_id="U_node_other", github_login="reviewer", zulip_user_id=None)

        with self.assertRaises(RegistrationLinkConflict):
            link_or_create_user_from_registration(
                zulip_user_id=101,
                zulip_full_name="Reviewer User",
                identity=self._identity(node_id="U_node_1"),
            )


class TestRegistrationLinkingInsertRace(TestCase):
    """Losing the insert race against the syncer creating the same GitHub user
    must fall through to the linking path instead of failing the registration."""

    def _identity(self) -> GitHubUserIdentity:
        return GitHubUserIdentity(
            github_user_id=123,
            github_node_id="U_node_1",
            github_login="reviewer",
            github_name="Reviewer User",
            github_avatar_url="https://example.com/avatar.png",
        )

    def test_link_converges_on_lost_insert_race(self) -> None:
        from unittest import mock

        # The "winner": the syncer ingested this login concurrently (no node id yet).
        winner = User.objects.create(github_login="Reviewer", github_node_id=None, zulip_user_id=None)

        # Simulate the loser's stale lookups: both select_for_update lookups miss
        # because the winner commits between them and our create.
        real_sfu = User.objects.select_for_update
        calls = {"n": 0}

        def stale_then_real_sfu(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] <= 2:
                return User.objects.none()
            return real_sfu(*args, **kwargs)

        with mock.patch.object(User.objects, "select_for_update", side_effect=stale_then_real_sfu):
            result = link_or_create_user_from_registration(
                zulip_user_id=101,
                zulip_full_name="Reviewer User",
                identity=self._identity(),
            )

        self.assertEqual(result.outcome, "linked_existing")
        self.assertEqual(User.objects.count(), 1)
        winner.refresh_from_db()
        self.assertEqual(winner.github_node_id, "U_node_1")
        self.assertEqual(winner.zulip_user_id, 101)
