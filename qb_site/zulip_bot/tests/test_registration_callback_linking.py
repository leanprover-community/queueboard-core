from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import Repository, ReviewerPreference, User
from zulip_bot.services.github_oauth import GitHubUserIdentity
from zulip_bot.services.registration_links import RegistrationLinkClaims, issue_registration_token
from zulip_bot.services.registration_oauth_state import RegistrationOAuthStateClaims, issue_registration_oauth_state


@override_settings(
    GITHUB_OAUTH_CLIENT_ID="client-id",
    GITHUB_OAUTH_CLIENT_SECRET="client-secret",
    GITHUB_OAUTH_REDIRECT_URI="https://queueboard.example/api/zulip/register/github/callback/",
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
        ):
            response = self.client.get(
                reverse("zulip-register-github-callback"),
                data={"state": state, "code": "oauth-code"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Link outcome")
        self.assertContains(response, "Preference bootstrap complete")
        user = User.objects.get(github_node_id="U_node_1")
        self.assertEqual(user.zulip_user_id, 101)
        self.assertEqual(ReviewerPreference.objects.filter(user=user).count(), 1)

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
        ):
            response = self.client.get(
                reverse("zulip-register-github-callback"),
                data={"state": state, "code": "oauth-code"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "created preferences: <code>0</code>")
        self.assertEqual(ReviewerPreference.objects.filter(user=user, repository=repo).count(), 1)
