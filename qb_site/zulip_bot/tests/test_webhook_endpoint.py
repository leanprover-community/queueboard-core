from __future__ import annotations

from django.test import TestCase, override_settings
from django.urls import reverse

from zulip_bot.tests.webhook_test_utils import WebhookTestMixin


@override_settings(ZULIP_WEBHOOK_TOKEN="test-token")
class TestZulipWebhookEndpoint(WebhookTestMixin, TestCase):
    def test_method_not_allowed(self) -> None:
        response = self.client.get(reverse("zulip-webhook"))
        self.assertEqual(response.status_code, 405)

    def test_rejects_invalid_token(self) -> None:
        result = self._post_payload(self._payload(content="help", token="wrong", message_type="private"))
        self.assertEqual(result["status"], 403)

    @override_settings(
        ZULIP_COMMAND_POLICY={
            "help": {"allowed_groups": ["all"], "allowed_contexts": ["stream:5"]},
            "echo": {"allowed_groups": ["all"], "allowed_contexts": ["stream:5"]},
        }
    )
    def test_unknown_command_returns_help(self) -> None:
        result = self._post_payload(self._payload(content="unknown", id=12, stream_id=5))
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["json"]["type"], "private")
        self.assertIn("Unknown command", result["json"]["content"])
        self.assertIn("- help: List supported commands.", result["json"]["content"])

    @override_settings(
        ZULIP_COMMAND_POLICY={
            "help": {"allowed_groups": ["all"], "allowed_contexts": ["dm"]},
            "echo": {"allowed_groups": ["all"], "allowed_contexts": ["dm"]},
        }
    )
    def test_unknown_command_ignored_when_no_commands_allowed(self) -> None:
        result = self._post_payload(self._payload(content="unknown", id=14, stream_id=8))
        self.assertEqual(result["status"], 200)
        self.assertIsNone(result["json"])

    @override_settings(
        ZULIP_COMMAND_POLICY={
            "echo": {"allowed_groups": ["all"], "allowed_contexts": ["all"]},
        }
    )
    def test_bot_sender_is_ignored(self) -> None:
        self.mock_is_bot_sender.return_value = True
        result = self._post_payload(self._payload(content="echo hello world", id=20, stream_id=5, sender_id=42))
        self.assertEqual(result["status"], 200)
        self.assertIsNone(result["json"])

    def test_invalid_json_payload_returns_400(self) -> None:
        response = self.client.post(
            reverse("zulip-webhook"),
            data="{",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload["error"], "Invalid payload")
        self.assertIn("invalid_json", payload["errors"][0])
