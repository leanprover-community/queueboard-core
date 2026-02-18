from __future__ import annotations

from django.test import TestCase

from core.models import User
from zulip_bot.services.github_oauth import GitHubUserIdentity
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
