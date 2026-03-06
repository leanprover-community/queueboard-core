from __future__ import annotations

import hashlib
import hmac

from django.test import SimpleTestCase, override_settings
from django.urls import reverse


def _signature(secret: str, payload: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class TestGitHubWebhookEndpoint(SimpleTestCase):
    def test_method_not_allowed(self) -> None:
        response = self.client.get(reverse("github-webhook"))
        self.assertEqual(response.status_code, 405)

    @override_settings(SYNCER_GITHUB_WEBHOOK_ENABLED=False, GITHUB_WEBHOOK_SECRET="test-secret")
    def test_disabled_returns_404(self) -> None:
        response = self.client.post(reverse("github-webhook"), data=b"{}", content_type="application/json")
        self.assertEqual(response.status_code, 404)

    @override_settings(SYNCER_GITHUB_WEBHOOK_ENABLED=True, GITHUB_WEBHOOK_SECRET="")
    def test_missing_secret_returns_503(self) -> None:
        response = self.client.post(reverse("github-webhook"), data=b"{}", content_type="application/json")
        self.assertEqual(response.status_code, 503)

    @override_settings(SYNCER_GITHUB_WEBHOOK_ENABLED=True, GITHUB_WEBHOOK_SECRET="test-secret")
    def test_invalid_signature_returns_403(self) -> None:
        response = self.client.post(
            reverse("github-webhook"),
            data=b'{"zen":"hi"}',
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256="sha256=bad",
            HTTP_X_GITHUB_EVENT="ping",
            HTTP_X_GITHUB_DELIVERY="delivery-1",
        )
        self.assertEqual(response.status_code, 403)

    @override_settings(SYNCER_GITHUB_WEBHOOK_ENABLED=True, GITHUB_WEBHOOK_SECRET="test-secret")
    def test_valid_signature_returns_202(self) -> None:
        payload = b'{"zen":"hi"}'
        response = self.client.post(
            reverse("github-webhook"),
            data=payload,
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=_signature("test-secret", payload),
            HTTP_X_GITHUB_EVENT="ping",
            HTTP_X_GITHUB_DELIVERY="delivery-2",
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {"status": "accepted"})
