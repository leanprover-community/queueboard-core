from __future__ import annotations

from django.test import SimpleTestCase, TestCase, override_settings

from core.models import User
from core.services.github_identity import resolve_user_from_identity
from core.services.github_oauth import GitHubUserIdentity
from core.services.oauth_state import (
    ConsoleOAuthStateClaims,
    issue_console_oauth_state,
    validate_console_oauth_state,
)
from core.services.site_urls import build_site_url, resolve_site_base_url


class ConsoleOAuthStateTests(SimpleTestCase):
    def test_console_state_carries_nonce_and_next(self) -> None:
        state = issue_console_oauth_state(claims=ConsoleOAuthStateClaims(nonce="abc", next="/console/"), now=1000)
        claims = validate_console_oauth_state(state, now=1100)
        self.assertEqual(claims.nonce, "abc")
        self.assertEqual(claims.next, "/console/")


class SiteUrlsTests(SimpleTestCase):
    @override_settings(QUEUEBOARD_BASE_URL="https://qb.example.com")
    def test_resolves_canonical_base(self) -> None:
        self.assertEqual(resolve_site_base_url(), "https://qb.example.com")
        self.assertEqual(build_site_url("/console/"), "https://qb.example.com/console/")

    @override_settings(QUEUEBOARD_BASE_URL="")
    def test_empty_returns_relative_path(self) -> None:
        self.assertEqual(resolve_site_base_url(), "")
        self.assertEqual(build_site_url("/console/"), "/console/")


class ResolveUserFromIdentityTests(TestCase):
    def _identity(self, *, node="MDQ6VXNlcjE=", login="alice", name="Alice", avatar="http://a") -> GitHubUserIdentity:
        return GitHubUserIdentity(
            github_user_id=1,
            github_node_id=node,
            github_login=login,
            github_name=name,
            github_avatar_url=avatar,
        )

    def test_matches_by_node_id_and_refreshes_login(self) -> None:
        existing = User.objects.create(github_node_id="MDQ6VXNlcjE=", github_login="old-login")
        user = resolve_user_from_identity(self._identity(login="new-login"))
        self.assertEqual(user.id, existing.id)
        self.assertEqual(user.github_login, "new-login")

    def test_matches_by_login_case_insensitively(self) -> None:
        existing = User.objects.create(github_login="Alice")
        user = resolve_user_from_identity(self._identity(login="alice", node="MDQ6VXNlcjE="))
        self.assertEqual(user.id, existing.id)
        # node id backfilled on the previously node-less row
        self.assertEqual(user.github_node_id, "MDQ6VXNlcjE=")

    def test_returns_none_and_creates_nothing_when_absent(self) -> None:
        # Resolve-only by construction: an unknown identity must not mint a core.User row
        # (console gating, doc 050 review).
        user = resolve_user_from_identity(self._identity(login="stranger"))
        self.assertIsNone(user)
        self.assertFalse(User.objects.filter(github_login__iexact="stranger").exists())

    def test_returns_existing(self) -> None:
        existing = User.objects.create(github_node_id="MDQ6VXNlcjE=", github_login="alice")
        user = resolve_user_from_identity(self._identity(login="alice"))
        self.assertEqual(user.id, existing.id)

    def test_recycled_login_does_not_resolve_to_previous_owner(self) -> None:
        # 'alice' renamed away; a different GitHub account (new node id) now holds the login. The
        # login fallback must not hand the new holder the previous owner's user (console takeover).
        previous_owner = User.objects.create(github_node_id="MDQ6VXNlcjE=", github_login="alice")
        user = resolve_user_from_identity(self._identity(node="MDQ6VXNlcjI=", login="alice"))
        self.assertIsNone(user)
        previous_owner.refresh_from_db()
        self.assertEqual(previous_owner.github_node_id, "MDQ6VXNlcjE=")
        self.assertEqual(previous_owner.github_login, "alice")
