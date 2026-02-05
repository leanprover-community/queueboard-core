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
        data: dict | None = None
        if response.content:
            data = response.json()
        return {"status": response.status_code, "json": data}

    def test_rejects_invalid_token(self) -> None:
        result = self._post_payload({"token": "wrong"})
        self.assertEqual(result["status"], 403)

    def test_no_policy_defaults_to_denied(self) -> None:
        payload = {
            "token": "test-token",
            "message": {
                "content": "help",
                "id": 9,
                "stream_id": 5,
                "type": "stream",
                "subject": "topic",
            },
        }
        result = self._post_payload(payload)
        self.assertEqual(result["status"], 200)
        self.assertIsNone(result["json"])

    @override_settings(
        ZULIP_COMMAND_POLICY={
            "help": {"allowed_contexts": ["stream:5"]},
            "echo": {"allowed_contexts": ["stream:5"]},
        }
    )
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
        self.assertEqual(
            result["json"],
            {
                "content": "Available commands:\n- echo: Repeat the provided text.\n- help: List supported commands.",
                "type": "private",
            },
        )

    @override_settings(
        ZULIP_COMMAND_POLICY={
            "help": {"allowed_contexts": ["stream:5"]},
            "echo": {"allowed_contexts": ["stream:5"]},
        }
    )
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

    @override_settings(
        ZULIP_COMMAND_POLICY={
            "help": {"allowed_contexts": ["stream:5"]},
            "echo": {"allowed_contexts": ["stream:5"]},
        }
    )
    def test_unknown_command_returns_help(self) -> None:
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
        self.assertIn("Available commands:", result["json"]["content"])

    @override_settings(
        ZULIP_COMMAND_POLICY={
            "help": {"allowed_contexts": ["stream:5"]},
        }
    )
    def test_missing_policy_entry_denies_command(self) -> None:
        payload = {
            "token": "test-token",
            "message": {
                "content": "echo hi",
                "id": 12,
                "stream_id": 5,
                "type": "stream",
                "subject": "topic",
            },
        }
        result = self._post_payload(payload)
        self.assertEqual(result["status"], 200)
        self.assertIsNone(result["json"])

    @override_settings(
        ZULIP_COMMAND_POLICY={
            "help": {"allowed_contexts": ["dm"]},
            "echo": {"allowed_contexts": ["dm"]},
        }
    )
    def test_disallowed_context_is_ignored(self) -> None:
        payload = {
            "token": "test-token",
            "message": {
                "content": "echo hi",
                "id": 13,
                "stream_id": 8,
                "type": "stream",
                "subject": "topic",
            },
        }
        result = self._post_payload(payload)
        self.assertEqual(result["status"], 200)
        self.assertIsNone(result["json"])

    @override_settings(
        ZULIP_COMMAND_POLICY={
            "help": {"allowed_contexts": ["dm"]},
            "echo": {"allowed_contexts": ["dm"]},
        }
    )
    def test_unknown_command_ignored_when_no_commands_allowed(self) -> None:
        payload = {
            "token": "test-token",
            "message": {
                "content": "unknown",
                "id": 14,
                "stream_id": 8,
                "type": "stream",
                "subject": "topic",
            },
        }
        result = self._post_payload(payload)
        self.assertEqual(result["status"], 200)
        self.assertIsNone(result["json"])

    @override_settings(
        ZULIP_COMMAND_POLICY={
            "help": {"allowed_contexts": ["dm"]},
            "echo": {"allowed_contexts": ["dm", "stream:5"]},
        }
    )
    def test_help_lists_only_allowed_commands(self) -> None:
        payload = {
            "token": "test-token",
            "message": {
                "content": "help",
                "id": 15,
                "stream_id": 5,
                "type": "stream",
                "subject": "topic",
            },
        }
        result = self._post_payload(payload)
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["json"]["type"], "private")
        self.assertIn("- echo:", result["json"]["content"])
        self.assertNotIn("- help:", result["json"]["content"])

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
