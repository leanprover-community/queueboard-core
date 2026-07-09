from __future__ import annotations

from django.test import SimpleTestCase, TestCase, override_settings

from core.models import User
from core.services.github_identity import resolve_or_create_user_from_identity
from core.services.github_oauth import GitHubUserIdentity
from core.services.oauth_state import (
    ConsoleOAuthStateClaims,
    SignedStateExpired,
    SignedStateInvalid,
    issue_console_oauth_state,
    issue_signed_state,
    read_signed_state,
    validate_console_oauth_state,
)
from core.services.site_urls import build_site_url, resolve_site_base_url


class SignedStateTests(SimpleTestCase):
    def test_round_trip(self) -> None:
        state = issue_signed_state({"k": "v"}, secret="s", salt="salt", ttl_seconds=600, now=1000)
        payload = read_signed_state(state, secret="s", salt="salt", now=1100)
        self.assertEqual(payload["k"], "v")
        self.assertEqual(payload["iat"], 1000)
        self.assertEqual(payload["exp"], 1600)

    def test_expired(self) -> None:
        state = issue_signed_state({"k": "v"}, secret="s", salt="salt", ttl_seconds=600, now=1000)
        with self.assertRaises(SignedStateExpired):
            read_signed_state(state, secret="s", salt="salt", now=1601)

    def test_wrong_secret_is_invalid(self) -> None:
        state = issue_signed_state({"k": "v"}, secret="s", salt="salt", ttl_seconds=600, now=1000)
        with self.assertRaises(SignedStateInvalid):
            read_signed_state(state, secret="other", salt="salt", now=1100)

    def test_tampered_is_invalid(self) -> None:
        with self.assertRaises(SignedStateInvalid):
            read_signed_state("not-a-real-token", secret="s", salt="salt", now=1100)

    def test_console_state_carries_nonce_and_next(self) -> None:
        state = issue_console_oauth_state(claims=ConsoleOAuthStateClaims(nonce="abc", next="/console/"), now=1000)
        claims = validate_console_oauth_state(state, now=1100)
        self.assertEqual(claims.nonce, "abc")
        self.assertEqual(claims.next, "/console/")


class SiteUrlsTests(SimpleTestCase):
    @override_settings(QUEUEBOARD_BASE_URL="https://qb.example.com", ZULIP_PREFS_URL_BASE="https://legacy.example.com")
    def test_prefers_canonical(self) -> None:
        self.assertEqual(resolve_site_base_url(), "https://qb.example.com")
        self.assertEqual(build_site_url("/console/"), "https://qb.example.com/console/")

    @override_settings(QUEUEBOARD_BASE_URL="", ZULIP_PREFS_URL_BASE="https://legacy.example.com")
    def test_falls_back_to_legacy(self) -> None:
        self.assertEqual(resolve_site_base_url(), "https://legacy.example.com")

    @override_settings(QUEUEBOARD_BASE_URL="", ZULIP_PREFS_URL_BASE="")
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

    def test_creates_when_absent(self) -> None:
        user = resolve_or_create_user_from_identity(self._identity())
        self.assertEqual(user.github_login, "alice")
        self.assertEqual(user.github_node_id, "MDQ6VXNlcjE=")

    def test_matches_by_node_id_and_refreshes_login(self) -> None:
        existing = User.objects.create(github_node_id="MDQ6VXNlcjE=", github_login="old-login")
        user = resolve_or_create_user_from_identity(self._identity(login="new-login"))
        self.assertEqual(user.id, existing.id)
        self.assertEqual(user.github_login, "new-login")

    def test_matches_by_login_case_insensitively(self) -> None:
        existing = User.objects.create(github_login="Alice")
        user = resolve_or_create_user_from_identity(self._identity(login="alice", node="MDQ6VXNlcjE="))
        self.assertEqual(user.id, existing.id)
        # node id backfilled on the previously node-less row
        self.assertEqual(user.github_node_id, "MDQ6VXNlcjE=")

    def test_resolve_only_returns_none_when_absent(self) -> None:
        # create=False must not mint a row for an unknown identity (console gating, doc 050 review).
        user = resolve_or_create_user_from_identity(self._identity(login="stranger"), create=False)
        self.assertIsNone(user)
        self.assertFalse(User.objects.filter(github_login__iexact="stranger").exists())

    def test_resolve_only_returns_existing(self) -> None:
        existing = User.objects.create(github_node_id="MDQ6VXNlcjE=", github_login="alice")
        user = resolve_or_create_user_from_identity(self._identity(login="alice"), create=False)
        self.assertEqual(user.id, existing.id)
