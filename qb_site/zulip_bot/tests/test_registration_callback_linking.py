from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from console.session import SESSION_USER_KEY
from core.models import Repository, ReviewerPreference, User
from core.services.github_oauth import GitHubUserIdentity
from zulip_bot.services.registration_links import RegistrationLinkClaims, issue_registration_token
from zulip_bot.services.registration_oauth_state import RegistrationOAuthStateClaims, issue_registration_oauth_state
from zulip_bot.services.zulip_client import ZulipApiError


@override_settings(
    GITHUB_OAUTH_CLIENT_ID="client-id",
    GITHUB_OAUTH_CLIENT_SECRET="client-secret",
    QUEUEBOARD_BASE_URL="https://queueboard.example",
)
class TestRegistrationCallbackLinking(TestCase):
    def _token_and_state(self, *, zulip_user_id: int = 101, nonce: str = "nonce-123") -> tuple[str, str]:
        token = issue_registration_token(
            claims=RegistrationLinkClaims(
                zulip_user_id=zulip_user_id,
                sender_email="reviewer@example.com",
                sender_full_name="Reviewer User",
                nonce=nonce,
            )
        )
        state = issue_registration_oauth_state(
            claims=RegistrationOAuthStateClaims(
                registration_token=token,
                registration_nonce=nonce,
            )
        )
        return token, state

    def _identity(self, *, node_id: str = "U_node_1", login: str = "reviewer") -> GitHubUserIdentity:
        return GitHubUserIdentity(
            github_user_id=123,
            github_node_id=node_id,
            github_login=login,
            github_name="Reviewer User",
            github_avatar_url="https://example.com/avatar.png",
        )

    def test_callback_creates_and_links_user(self) -> None:
        Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master", is_active=True)
        Repository.objects.create(owner="leanprover", name="stdlib", default_branch="master", is_active=False)
        _token, state = self._token_and_state(zulip_user_id=101)
        with (
            patch("zulip_bot.views.GitHubOAuthClient.exchange_code_for_access_token", return_value="access-token"),
            patch("zulip_bot.views.GitHubOAuthClient.fetch_user_identity", return_value=self._identity()),
            patch("zulip_bot.views.ZulipClient") as mock_zulip_client_cls,
        ):
            response = self.client.get(
                reverse("zulip-register-github-callback"),
                data={"state": state, "code": "oauth-code"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Link outcome")
        self.assertContains(response, "finalize your reviewer preferences")
        self.assertContains(response, "Edit Preferences Now")
        self.assertContains(response, "/api/zulip/prefs/")
        self.assertContains(response, "Sent a confirmation DM")
        user = User.objects.get(github_node_id="U_node_1")
        self.assertEqual(user.zulip_user_id, 101)
        self.assertEqual(ReviewerPreference.objects.filter(user=user).count(), 1)
        mock_zulip_client_cls.return_value.send_direct_message.assert_called_once()
        dm_kwargs = mock_zulip_client_cls.return_value.send_direct_message.call_args.kwargs
        self.assertEqual(dm_kwargs["to"], [101])
        self.assertIn("Successfully linked your Zulip account with GitHub user `reviewer`", dm_kwargs["content"])
        self.assertIn("[finalize your reviewer preferences](", dm_kwargs["content"])
        self.assertIn("<time:", dm_kwargs["content"])

    @override_settings(CONSOLE_PREFS_ENABLED=True)
    def test_callback_hands_off_to_the_console_and_opens_a_session(self) -> None:
        # With the console owning preferences, registration advertises the stable URL and promotes the
        # console session: the reviewer just proved this GitHub identity in this browser, and the
        # registration token proved their Zulip identity (design doc 022, phase 2).
        Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master", is_active=True)
        _token, state = self._token_and_state(zulip_user_id=101)
        with (
            patch("zulip_bot.views.GitHubOAuthClient.exchange_code_for_access_token", return_value="access-token"),
            patch("zulip_bot.views.GitHubOAuthClient.fetch_user_identity", return_value=self._identity()),
            patch("zulip_bot.views.ZulipClient") as mock_zulip_client_cls,
        ):
            response = self.client.get(
                reverse("zulip-register-github-callback"),
                data={"state": state, "code": "oauth-code"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "/console/preferences/")
        self.assertNotContains(response, "/api/zulip/prefs/")
        self.assertNotContains(response, "This link expires at")
        self.assertContains(response, "signed in on this browser")

        user = User.objects.get(github_node_id="U_node_1")
        self.assertEqual(self.client.session.get(SESSION_USER_KEY), user.id)

        dm_content = mock_zulip_client_cls.return_value.send_direct_message.call_args.kwargs["content"]
        self.assertIn("https://queueboard.example/console/preferences/", dm_content)
        self.assertNotIn("/api/zulip/prefs/", dm_content)
        self.assertNotIn("expires at", dm_content)

    @override_settings(CONSOLE_PREFS_ENABLED=True)
    def test_callback_opens_no_session_when_there_is_nothing_to_edit(self) -> None:
        # No active repositories -> no preference rows -> the console would refuse them anyway.
        _token, state = self._token_and_state(zulip_user_id=101)
        with (
            patch("zulip_bot.views.GitHubOAuthClient.exchange_code_for_access_token", return_value="access-token"),
            patch("zulip_bot.views.GitHubOAuthClient.fetch_user_identity", return_value=self._identity()),
            patch("zulip_bot.views.ZulipClient"),
        ):
            response = self.client.get(
                reverse("zulip-register-github-callback"),
                data={"state": state, "code": "oauth-code"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No preferences are available to edit yet")
        self.assertIsNone(self.client.session.get(SESSION_USER_KEY))

    def test_callback_returns_conflict_page_for_existing_other_zulip_link(self) -> None:
        User.objects.create(github_node_id="U_node_1", github_login="reviewer", zulip_user_id=202)
        _token, state = self._token_and_state(zulip_user_id=101)
        with (
            patch("zulip_bot.views.GitHubOAuthClient.exchange_code_for_access_token", return_value="access-token"),
            patch("zulip_bot.views.GitHubOAuthClient.fetch_user_identity", return_value=self._identity()),
        ):
            response = self.client.get(
                reverse("zulip-register-github-callback"),
                data={"state": state, "code": "oauth-code"},
            )

        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "already linked to another Zulip user", status_code=403)

    def test_callback_bootstrap_is_idempotent_for_existing_preferences(self) -> None:
        repo = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master", is_active=True)
        user = User.objects.create(github_node_id="U_node_1", github_login="reviewer", zulip_user_id=101)
        ReviewerPreference.objects.create(user=user, repository=repo)
        _token, state = self._token_and_state(zulip_user_id=101)
        with (
            patch("zulip_bot.views.GitHubOAuthClient.exchange_code_for_access_token", return_value="access-token"),
            patch("zulip_bot.views.GitHubOAuthClient.fetch_user_identity", return_value=self._identity()),
            patch("zulip_bot.views.ZulipClient"),
        ):
            response = self.client.get(
                reverse("zulip-register-github-callback"),
                data={"state": state, "code": "oauth-code"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "created preferences: <code>0</code>")
        self.assertEqual(ReviewerPreference.objects.filter(user=user, repository=repo).count(), 1)

    def test_callback_succeeds_when_confirmation_dm_fails(self) -> None:
        Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master", is_active=True)
        _token, state = self._token_and_state(zulip_user_id=101)
        with (
            patch("zulip_bot.views.GitHubOAuthClient.exchange_code_for_access_token", return_value="access-token"),
            patch("zulip_bot.views.GitHubOAuthClient.fetch_user_identity", return_value=self._identity()),
            patch("zulip_bot.views.ZulipClient", side_effect=ZulipApiError("not configured")),
            patch("zulip_bot.views.logger.exception") as mock_logger_exception,
        ):
            response = self.client.get(
                reverse("zulip-register-github-callback"),
                data={"state": state, "code": "oauth-code"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Could not send confirmation DM in Zulip")
        mock_logger_exception.assert_called()
