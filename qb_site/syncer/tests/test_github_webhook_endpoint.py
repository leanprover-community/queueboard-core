from __future__ import annotations

import hashlib
import hmac
from unittest.mock import patch
from urllib.parse import urlencode

from django.db import IntegrityError

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
    def test_missing_signature_returns_403(self) -> None:
        response = self.client.post(
            reverse("github-webhook"),
            data=b'{"zen":"hi"}',
            content_type="application/json",
            HTTP_X_GITHUB_EVENT="ping",
            HTTP_X_GITHUB_DELIVERY="delivery-1b",
        )
        self.assertEqual(response.status_code, 403)

    @override_settings(SYNCER_GITHUB_WEBHOOK_ENABLED=True, GITHUB_WEBHOOK_SECRET="test-secret")
    def test_valid_signature_returns_202(self) -> None:
        payload = b'{"zen":"hi"}'
        with patch("syncer.views.GitHubWebhookDelivery.objects.create") as mock_create:
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
        self.assertEqual(mock_create.call_count, 1)

    @override_settings(SYNCER_GITHUB_WEBHOOK_ENABLED=True, GITHUB_WEBHOOK_SECRET="test-secret")
    def test_missing_delivery_id_returns_400(self) -> None:
        payload = b'{"zen":"hi"}'
        response = self.client.post(
            reverse("github-webhook"),
            data=payload,
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=_signature("test-secret", payload),
            HTTP_X_GITHUB_EVENT="ping",
        )
        self.assertEqual(response.status_code, 400)

    @override_settings(SYNCER_GITHUB_WEBHOOK_ENABLED=True, GITHUB_WEBHOOK_SECRET="test-secret")
    def test_duplicate_delivery_returns_202_duplicate(self) -> None:
        payload = b'{"action":"completed","repository":{"owner":{"login":"leanprover-community"},"name":"mathlib4"}}'
        with patch("syncer.views.GitHubWebhookDelivery.objects.create", side_effect=IntegrityError):
            response = self.client.post(
                reverse("github-webhook"),
                data=payload,
                content_type="application/json",
                HTTP_X_HUB_SIGNATURE_256=_signature("test-secret", payload),
                HTTP_X_GITHUB_EVENT="check_run",
                HTTP_X_GITHUB_DELIVERY="delivery-3",
            )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {"status": "duplicate"})

    @override_settings(SYNCER_GITHUB_WEBHOOK_ENABLED=True, GITHUB_WEBHOOK_SECRET="test-secret")
    def test_form_encoded_payload_parses_action_and_repo(self) -> None:
        inner = '{"action":"completed","repository":{"owner":{"login":"leanprover-community"},"name":"mathlib4"}}'
        payload = urlencode({"payload": inner}).encode("utf-8")
        with patch("syncer.views.GitHubWebhookDelivery.objects.create") as mock_create:
            response = self.client.post(
                reverse("github-webhook"),
                data=payload,
                content_type="application/x-www-form-urlencoded",
                HTTP_X_HUB_SIGNATURE_256=_signature("test-secret", payload),
                HTTP_X_GITHUB_EVENT="check_run",
                HTTP_X_GITHUB_DELIVERY="delivery-4",
            )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(mock_create.call_count, 1)
        kwargs = mock_create.call_args.kwargs
        self.assertEqual(kwargs["action"], "completed")
        self.assertEqual(kwargs["repository_owner"], "leanprover-community")
        self.assertEqual(kwargs["repository_name"], "mathlib4")
