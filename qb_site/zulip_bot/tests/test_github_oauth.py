from __future__ import annotations

from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from zulip_bot.services.github_oauth import GitHubOAuthClient, GitHubOAuthError


@override_settings(
    GITHUB_OAUTH_CLIENT_ID="client-id",
    GITHUB_OAUTH_CLIENT_SECRET="client-secret",
    GITHUB_OAUTH_REDIRECT_URI="https://queueboard.example/api/zulip/register/github/callback/",
)
class TestGitHubOAuthClient(SimpleTestCase):
    def _response(self, payload: dict) -> Mock:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = payload
        return response

    def test_build_authorize_url(self) -> None:
        client = GitHubOAuthClient()
        url = client.build_authorize_url(
            state="state-token",
            redirect_uri="https://queueboard.example/api/zulip/register/github/callback/",
        )

        self.assertIn("github.com/login/oauth/authorize", url)
        self.assertIn("client_id=client-id", url)
        self.assertIn("scope=read%3Auser", url)
        self.assertIn("state=state-token", url)

    def test_exchange_code_for_access_token(self) -> None:
        with patch(
            "zulip_bot.services.github_oauth.requests.post",
            return_value=self._response({"access_token": "token-123"}),
        ):
            client = GitHubOAuthClient()
            token = client.exchange_code_for_access_token(
                code="oauth-code",
                redirect_uri="https://queueboard.example/api/zulip/register/github/callback/",
            )

        self.assertEqual(token, "token-123")

    def test_fetch_user_identity(self) -> None:
        with patch(
            "zulip_bot.services.github_oauth.requests.get",
            return_value=self._response(
                {
                    "id": 123,
                    "node_id": "U_node",
                    "login": "reviewer",
                    "name": "Reviewer",
                    "avatar_url": "https://example.com/avatar.png",
                }
            ),
        ):
            client = GitHubOAuthClient()
            identity = client.fetch_user_identity(access_token="token-123")

        self.assertEqual(identity.github_user_id, 123)
        self.assertEqual(identity.github_node_id, "U_node")
        self.assertEqual(identity.github_login, "reviewer")

    @override_settings(GITHUB_OAUTH_CLIENT_ID="", GITHUB_OAUTH_CLIENT_SECRET="")
    def test_missing_oauth_config_raises(self) -> None:
        with self.assertRaises(GitHubOAuthError):
            GitHubOAuthClient()
