from __future__ import annotations

from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from core.services.github_app_tokens import GitHubAppTokenError
from core.services.github_operation_tokens import resolve_github_operation_token


class TestResolveGitHubOperationToken(SimpleTestCase):
    @override_settings(GITHUB_ASSIGNMENT_TOKEN="")
    def test_returns_app_token_when_available(self) -> None:
        provider = Mock()
        provider.get_token.return_value = "app-token"
        with patch("core.services.github_operation_tokens.get_default_github_app_token_provider", return_value=provider):
            token = resolve_github_operation_token(
                operation="assign_pr",
                owner="leanprover-community",
                repo="mathlib4",
                setting_token_names=("GITHUB_ASSIGNMENT_TOKEN",),
            )

        self.assertEqual(token, "app-token")
        provider.get_token.assert_called_once_with(
            operation="assign_pr",
            owner="leanprover-community",
            repo="mathlib4",
        )

    @override_settings(GITHUB_ASSIGNMENT_TOKEN="setting-token")
    def test_falls_back_to_named_setting_when_app_token_missing(self) -> None:
        provider = Mock()
        provider.get_token.return_value = None
        with patch("core.services.github_operation_tokens.get_default_github_app_token_provider", return_value=provider):
            token = resolve_github_operation_token(
                operation="assign_pr",
                owner="leanprover-community",
                repo="mathlib4",
                setting_token_names=("GITHUB_ASSIGNMENT_TOKEN",),
            )

        self.assertEqual(token, "setting-token")

    @override_settings(GITHUB_ASSIGNMENT_TOKEN="")
    def test_falls_back_to_env_tokens_when_app_and_setting_missing(self) -> None:
        provider = Mock()
        provider.get_token.return_value = None
        with (
            patch("core.services.github_operation_tokens.get_default_github_app_token_provider", return_value=provider),
            patch.dict("os.environ", {"GH_TOKEN": "gh-1, gh-2", "GITHUB_TOKEN": ""}, clear=False),
        ):
            token = resolve_github_operation_token(
                operation="assign_pr",
                owner="leanprover-community",
                repo="mathlib4",
                setting_token_names=("GITHUB_ASSIGNMENT_TOKEN",),
            )

        self.assertEqual(token, "gh-1")

    @override_settings(GITHUB_ASSIGNMENT_TOKEN="setting-token")
    def test_app_token_error_logs_and_falls_back(self) -> None:
        provider = Mock()
        provider.get_token.side_effect = GitHubAppTokenError(code="installation_not_found", message="not installed")
        with (
            patch("core.services.github_operation_tokens.get_default_github_app_token_provider", return_value=provider),
            patch("core.services.github_operation_tokens.log.warning") as mock_warning,
        ):
            token = resolve_github_operation_token(
                operation="assign_pr",
                owner="leanprover-community",
                repo="mathlib4",
                setting_token_names=("GITHUB_ASSIGNMENT_TOKEN",),
            )

        self.assertEqual(token, "setting-token")
        mock_warning.assert_called_once()

    @override_settings(GITHUB_ASSIGNMENT_TOKEN="")
    def test_no_operation_still_uses_fallback_chain(self) -> None:
        with patch.dict("os.environ", {"GH_TOKEN": "", "GITHUB_TOKEN": "gh-token"}, clear=False):
            token = resolve_github_operation_token(
                operation=None,
                owner="leanprover-community",
                repo="mathlib4",
                setting_token_names=("GITHUB_ASSIGNMENT_TOKEN",),
            )

        self.assertEqual(token, "gh-token")
