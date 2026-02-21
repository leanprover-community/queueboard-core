from __future__ import annotations

from datetime import timedelta, timezone as dt_timezone
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.test import SimpleTestCase
from django.utils import timezone

from core.services.github_app_tokens import GitHubAppInstallationTokenProvider


class TestGitHubAppInstallationTokenProvider(SimpleTestCase):
    def _response(self, *, status_code: int, payload: dict | None = None, text: str = "") -> Mock:
        response = Mock()
        response.status_code = status_code
        response.text = text
        response.json.return_value = payload or {}
        return response

    def _private_key_pem(self) -> str:
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        key_bytes = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        return key_bytes.decode("utf-8")

    def _iso_utc(self, value) -> str:
        return value.astimezone(dt_timezone.utc).isoformat().replace("+00:00", "Z")

    def test_mints_and_reuses_cached_installation_token(self) -> None:
        config = {
            "apps": [
                {
                    "name": "assign-app",
                    "app_id": 42,
                    "private_key": self._private_key_pem(),
                    "operations": ["assign_pr"],
                }
            ]
        }
        provider = GitHubAppInstallationTokenProvider(config=config)
        expires_at = self._iso_utc(timezone.now() + timedelta(hours=1))
        with (
            patch(
                "core.services.github_app_tokens.requests.get",
                return_value=self._response(status_code=200, payload={"id": 123}),
            ) as mock_get,
            patch(
                "core.services.github_app_tokens.requests.post",
                return_value=self._response(status_code=201, payload={"token": "inst-token-1", "expires_at": expires_at}),
            ) as mock_post,
        ):
            first = provider.get_token(operation="assign_pr", owner="leanprover-community", repo="mathlib4")
            second = provider.get_token(operation="assign_pr", owner="leanprover-community", repo="mathlib4")

        self.assertEqual(first, "inst-token-1")
        self.assertEqual(second, "inst-token-1")
        self.assertEqual(mock_get.call_count, 1)
        self.assertEqual(mock_post.call_count, 1)

    def test_refreshes_cached_token_when_expiring_within_skew(self) -> None:
        config = {
            "cache_skew_seconds": 60,
            "apps": [
                {
                    "name": "assign-app",
                    "app_id": 42,
                    "private_key": self._private_key_pem(),
                    "operations": ["assign_pr"],
                }
            ],
        }
        provider = GitHubAppInstallationTokenProvider(config=config)
        near_expiry = self._iso_utc(timezone.now() + timedelta(seconds=30))
        far_expiry = self._iso_utc(timezone.now() + timedelta(hours=1))

        with (
            patch(
                "core.services.github_app_tokens.requests.get",
                return_value=self._response(status_code=200, payload={"id": 123}),
            ) as mock_get,
            patch(
                "core.services.github_app_tokens.requests.post",
                side_effect=[
                    self._response(status_code=201, payload={"token": "inst-token-1", "expires_at": near_expiry}),
                    self._response(status_code=201, payload={"token": "inst-token-2", "expires_at": far_expiry}),
                ],
            ) as mock_post,
        ):
            first = provider.get_token(operation="assign_pr", owner="leanprover-community", repo="mathlib4")
            second = provider.get_token(operation="assign_pr", owner="leanprover-community", repo="mathlib4")

        self.assertEqual(first, "inst-token-1")
        self.assertEqual(second, "inst-token-2")
        self.assertEqual(mock_get.call_count, 1)
        self.assertEqual(mock_post.call_count, 2)

    def test_operation_mapping_selects_explicit_app(self) -> None:
        config = {
            "operation_app_map": {"assign_pr": "mapped-app"},
            "apps": [
                {
                    "name": "default-app",
                    "app_id": 1,
                    "private_key": "dummy",
                    "operations": ["assign_pr"],
                },
                {
                    "name": "mapped-app",
                    "app_id": 2,
                    "private_key": "dummy",
                    "operations": [],
                },
            ],
        }
        provider = GitHubAppInstallationTokenProvider(config=config)
        with (
            patch("core.services.github_app_tokens._build_github_app_jwt", side_effect=["jwt-install", "jwt-token"]),
            patch(
                "core.services.github_app_tokens.requests.get",
                return_value=self._response(status_code=200, payload={"id": 444}),
            ) as mock_get,
            patch(
                "core.services.github_app_tokens.requests.post",
                return_value=self._response(
                    status_code=201,
                    payload={"token": "mapped-token", "expires_at": self._iso_utc(timezone.now() + timedelta(hours=1))},
                ),
            ),
        ):
            token = provider.get_token(operation="assign_pr", owner="leanprover-community", repo="mathlib4")

        self.assertEqual(token, "mapped-token")
        self.assertEqual(mock_get.call_args.kwargs["headers"]["Authorization"], "Bearer jwt-install")
