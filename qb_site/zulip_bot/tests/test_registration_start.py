from __future__ import annotations

from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.test import TestCase, override_settings
from django.urls import reverse

from core.services.github_oauth import GitHubUserIdentity
from zulip_bot.services.registration_oauth_state import validate_registration_oauth_state
from zulip_bot.services.registration_links import RegistrationLinkClaims, issue_registration_token


class TestRegistrationStart(TestCase):
    def _token(self) -> str:
        return issue_registration_token(
            claims=RegistrationLinkClaims(
                zulip_user_id=101,
                sender_email="reviewer@example.com",
                sender_full_name="Reviewer User",
            )
        )

    @override_settings(GITHUB_OAUTH_CLIENT_ID="client-id", GITHUB_OAUTH_CLIENT_SECRET="client-secret")
    def test_get_with_valid_token_renders_start_page(self) -> None:
        token = self._token()
        response = self.client.get(reverse("zulip-register-start", kwargs={"token": token}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Queueboard registration")
        self.assertContains(response, "Continue with GitHub")
        self.assertContains(response, "This link expires at")
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_invalid_token_returns_forbidden(self) -> None:
        response = self.client.get(reverse("zulip-register-start", kwargs={"token": "not-a-token"}))

        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "invalid", status_code=403)

    def test_expired_token_returns_forbidden(self) -> None:
        with patch("zulip_bot.services.registration_links.time.time", return_value=1_700_000_000):
            token = self._token()
        with patch("zulip_bot.services.registration_links.time.time", return_value=1_700_000_000 + 1_900):
            response = self.client.get(reverse("zulip-register-start", kwargs={"token": token}))

        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "expired", status_code=403)

    @override_settings(GITHUB_OAUTH_CLIENT_ID="", GITHUB_OAUTH_CLIENT_SECRET="")
    def test_get_with_oauth_not_configured_shows_message(self) -> None:
        token = self._token()
        response = self.client.get(reverse("zulip-register-start", kwargs={"token": token}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "not configured")

    @override_settings(
        GITHUB_OAUTH_CLIENT_ID="client-id",
        GITHUB_OAUTH_CLIENT_SECRET="client-secret",
        QUEUEBOARD_BASE_URL="https://queueboard.example",
    )
    def test_register_github_start_redirects_to_github_authorize(self) -> None:
        token = self._token()
        response = self.client.get(reverse("zulip-register-github-start", kwargs={"token": token}))

        self.assertEqual(response.status_code, 302)
        parsed = urlparse(response["Location"])
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "github.com")
        self.assertEqual(parsed.path, "/login/oauth/authorize")
        query = parse_qs(parsed.query)
        self.assertEqual(query["client_id"], ["client-id"])
        self.assertEqual(query["redirect_uri"], ["https://queueboard.example/api/zulip/register/github/callback/"])
        self.assertEqual(query["scope"], ["read:user"])
        self.assertEqual(len(query["state"]), 1)
        state_claims = validate_registration_oauth_state(query["state"][0])
        self.assertEqual(state_claims.registration_token, token)

    @override_settings(GITHUB_OAUTH_CLIENT_ID="", GITHUB_OAUTH_CLIENT_SECRET="")
    def test_register_github_start_without_oauth_config_returns_forbidden(self) -> None:
        token = self._token()
        response = self.client.get(reverse("zulip-register-github-start", kwargs={"token": token}))

        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "not configured", status_code=403)

    @override_settings(
        GITHUB_OAUTH_CLIENT_ID="client-id",
        GITHUB_OAUTH_CLIENT_SECRET="client-secret",
        QUEUEBOARD_BASE_URL="https://queueboard.example",
    )
    def test_register_github_callback_with_missing_params_returns_forbidden(self) -> None:
        response = self.client.get(reverse("zulip-register-github-callback"))

        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "invalid", status_code=403)

    @override_settings(
        GITHUB_OAUTH_CLIENT_ID="client-id",
        GITHUB_OAUTH_CLIENT_SECRET="client-secret",
        QUEUEBOARD_BASE_URL="https://queueboard.example",
    )
    def test_register_github_callback_with_valid_state_renders_verified_page(self) -> None:
        token = self._token()
        start_response = self.client.get(reverse("zulip-register-github-start", kwargs={"token": token}))
        parsed = urlparse(start_response["Location"])
        query = parse_qs(parsed.query)
        state = query["state"][0]

        with (
            patch("zulip_bot.views.GitHubOAuthClient.exchange_code_for_access_token", return_value="access-token"),
            patch(
                "zulip_bot.views.GitHubOAuthClient.fetch_user_identity",
                return_value=GitHubUserIdentity(
                    github_user_id=123,
                    github_node_id="U_node",
                    github_login="reviewer",
                    github_name="Reviewer User",
                    github_avatar_url="https://example.com/avatar.png",
                ),
            ),
            patch("zulip_bot.views.ZulipClient"),
        ):
            response = self.client.get(
                reverse("zulip-register-github-callback"),
                data={"state": state, "code": "oauth-code"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "GitHub verification complete")
        self.assertContains(response, "reviewer")

    @override_settings(
        GITHUB_OAUTH_CLIENT_ID="client-id",
        GITHUB_OAUTH_CLIENT_SECRET="client-secret",
        QUEUEBOARD_BASE_URL="https://queueboard.example",
    )
    def test_register_github_callback_with_invalid_state_returns_forbidden(self) -> None:
        response = self.client.get(
            reverse("zulip-register-github-callback"),
            data={"state": "not-a-state", "code": "oauth-code"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "invalid", status_code=403)
