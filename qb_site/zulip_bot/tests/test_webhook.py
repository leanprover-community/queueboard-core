from __future__ import annotations

import json

from django.test import TestCase, override_settings
from django.urls import reverse


@override_settings(ZULIP_WEBHOOK_TOKEN="test-token")
class TestZulipWebhook(TestCase):
    def _post_payload(self, payload: dict) -> dict:
        response = self.client.post(
            reverse("zulip-webhook"),
            data=json.dumps(payload),
            content_type="application/json",
        )
        return {"status": response.status_code, "json": response.json()}

    def test_rejects_invalid_token(self) -> None:
        result = self._post_payload({"token": "wrong"})
        self.assertEqual(result["status"], 403)

    def test_help_command_lists_commands(self) -> None:
        payload = {
            "token": "test-token",
            "message": {
                "content": "help",
                "id": 10,
                "stream_id": 5,
                "type": "stream",
                "subject": "topic",
            },
        }
        result = self._post_payload(payload)
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["json"]["type"], "private")
        self.assertIn("help", result["json"]["content"])
        self.assertIn("echo", result["json"]["content"])

    def test_echo_command_repeats_text(self) -> None:
        payload = {
            "token": "test-token",
            "message": {
                "content": "echo hello world",
                "id": 11,
                "stream_id": 5,
                "type": "stream",
                "subject": "topic",
            },
        }
        result = self._post_payload(payload)
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["json"]["type"], "private")
        self.assertEqual(result["json"]["content"], "hello world")

    def test_unknown_command_defaults_to_private(self) -> None:
        payload = {
            "token": "test-token",
            "message": {
                "content": "unknown",
                "id": 12,
                "stream_id": 5,
                "type": "stream",
                "subject": "topic",
            },
        }
        result = self._post_payload(payload)
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["json"]["type"], "private")
        self.assertIn("Unknown command", result["json"]["content"])
