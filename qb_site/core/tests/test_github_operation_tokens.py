from __future__ import annotations

from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from core.services.github_app_tokens import GitHubAppTokenError
from core.services.github_operation_tokens import resolve_github_app_operation_token


class TestResolveGitHubAppOperationToken(SimpleTestCase):
    def test_returns_app_token_when_available(self) -> None:
        provider = Mock()
        provider.get_token.return_value = "app-token"
        with patch("core.services.github_operation_tokens.get_default_github_app_token_provider", return_value=provider):
            token = resolve_github_app_operation_token(
                operation="assign_pr",
                owner="leanprover-community",
                repo="mathlib4",
            )

        self.assertEqual(token, "app-token")
        provider.get_token.assert_called_once_with(
            operation="assign_pr",
            owner="leanprover-community",
            repo="mathlib4",
        )

    def test_returns_none_when_no_app_token_resolved(self) -> None:
        provider = Mock()
        provider.get_token.return_value = None
        with patch("core.services.github_operation_tokens.get_default_github_app_token_provider", return_value=provider):
            token = resolve_github_app_operation_token(
                operation="assign_pr",
                owner="leanprover-community",
                repo="mathlib4",
            )

        self.assertIsNone(token)

    def test_logs_and_returns_none_on_app_token_error(self) -> None:
        provider = Mock()
        provider.get_token.side_effect = GitHubAppTokenError(code="installation_not_found", message="not installed")
        with (
            patch("core.services.github_operation_tokens.get_default_github_app_token_provider", return_value=provider),
            patch("core.services.github_operation_tokens.log.warning") as mock_warning,
        ):
            token = resolve_github_app_operation_token(
                operation="assign_pr",
                owner="leanprover-community",
                repo="mathlib4",
            )

        self.assertIsNone(token)
        mock_warning.assert_called_once()

    def test_no_operation_returns_none(self) -> None:
        token = resolve_github_app_operation_token(
            operation=None,
            owner="leanprover-community",
            repo="mathlib4",
        )

        self.assertIsNone(token)
