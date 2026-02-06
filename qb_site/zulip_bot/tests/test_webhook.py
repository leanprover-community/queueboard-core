from __future__ import annotations

import json
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse


@override_settings(ZULIP_WEBHOOK_TOKEN="test-token")
class TestZulipWebhook(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._sender_classifier_patcher = patch("zulip_bot.views.SenderClassifier.is_bot_sender", return_value=False)
        self.mock_is_bot_sender = self._sender_classifier_patcher.start()

    def tearDown(self) -> None:
        self._sender_classifier_patcher.stop()
        super().tearDown()

    def _payload(
        self, *, content: str, token: str = "test-token", message_type: str = "stream", **message_overrides: object
    ) -> dict:
        message: dict[str, object] = {
            "id": 999,
            "type": message_type,
            "content": content,
            "sender_id": 101,
            "sender_email": "reviewer@example.com",
            "sender_full_name": "Reviewer User",
        }
        if message_type == "stream":
            message["stream_id"] = 5
            message["subject"] = "topic"
        message.update(message_overrides)
        return {"token": token, "message": message}

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
        result = self._post_payload(self._payload(content="help", token="wrong", message_type="private"))
        self.assertEqual(result["status"], 403)

    def test_no_policy_defaults_to_denied(self) -> None:
        result = self._post_payload(self._payload(content="help", id=9, stream_id=5))
        self.assertEqual(result["status"], 200)
        self.assertIsNone(result["json"])

    @override_settings(
        ZULIP_COMMAND_POLICY={
            "help": {"allowed_groups": ["all"], "allowed_contexts": ["stream:5"]},
            "echo": {"allowed_groups": ["all"], "allowed_contexts": ["stream:5"]},
        }
    )
    def test_help_command_lists_commands(self) -> None:
        result = self._post_payload(self._payload(content="help", id=10, stream_id=5))
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["json"]["type"], "private")
        self.assertIn("- echo: Repeat the provided text.", result["json"]["content"])
        self.assertIn("- help: List supported commands.", result["json"]["content"])

    @override_settings(
        ZULIP_COMMAND_POLICY={
            "help": {"allowed_groups": ["all"], "allowed_contexts": ["stream:5"]},
            "echo": {"allowed_groups": ["all"], "allowed_contexts": ["stream:5"]},
        }
    )
    def test_echo_command_repeats_text(self) -> None:
        result = self._post_payload(self._payload(content="echo hello world", id=11, stream_id=5))
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["json"]["type"], "private")
        self.assertEqual(result["json"]["content"], "hello world")

    @override_settings(
        ZULIP_COMMAND_POLICY={
            "echo": {"allowed_groups": ["all"], "allowed_contexts": ["all"]},
        }
    )
    def test_mention_prefix_is_ignored_for_command_parse(self) -> None:
        result = self._post_payload(self._payload(content="@**queueboard-bot** echo hello world", id=19, stream_id=5))
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["json"]["type"], "private")
        self.assertEqual(result["json"]["content"], "hello world")

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
            "help": {"allowed_groups": ["all"], "allowed_contexts": ["stream:5"]},
        }
    )
    def test_missing_policy_entry_denies_command(self) -> None:
        result = self._post_payload(self._payload(content="echo hi", id=12, stream_id=5))
        self.assertEqual(result["status"], 200)
        self.assertIsNone(result["json"])

    @override_settings(
        ZULIP_COMMAND_POLICY={
            "echo": {"allowed_contexts": []},
        }
    )
    def test_empty_allowed_contexts_means_denied(self) -> None:
        result = self._post_payload(self._payload(content="echo hi", id=16, stream_id=8))
        self.assertEqual(result["status"], 200)
        self.assertIsNone(result["json"])

    @override_settings(
        ZULIP_COMMAND_POLICY={
            "echo": {"allowed_groups": ["all"], "allowed_contexts": ["all"]},
        }
    )
    def test_allowed_contexts_all_means_unrestricted(self) -> None:
        result = self._post_payload(self._payload(content="echo hi", id=17, stream_id=8))
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["json"]["type"], "private")
        self.assertEqual(result["json"]["content"], "hi")

    @override_settings(
        ZULIP_COMMAND_POLICY={
            "echo": {"allowed_groups": ["all"], "allowed_contexts": ["all"]},
        }
    )
    def test_allowed_groups_all_means_unrestricted(self) -> None:
        result = self._post_payload(self._payload(content="echo hi", id=18, stream_id=8))
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["json"]["type"], "private")
        self.assertEqual(result["json"]["content"], "hi")

    @override_settings(
        ZULIP_COMMAND_POLICY={
            "help": {"allowed_groups": ["all"], "allowed_contexts": ["dm"]},
            "echo": {"allowed_groups": ["all"], "allowed_contexts": ["dm"]},
        }
    )
    def test_disallowed_context_is_ignored(self) -> None:
        result = self._post_payload(self._payload(content="echo hi", id=13, stream_id=8))
        self.assertEqual(result["status"], 200)
        self.assertIsNone(result["json"])

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
            "help": {"allowed_groups": ["all"], "allowed_contexts": ["stream:5"]},
            "echo": {"allowed_groups": ["all"], "allowed_contexts": ["dm"]},
        }
    )
    def test_help_lists_only_allowed_commands(self) -> None:
        result = self._post_payload(self._payload(content="help", id=15, stream_id=5))
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["json"]["type"], "private")
        self.assertNotIn("- echo:", result["json"]["content"])
        self.assertIn("- help:", result["json"]["content"])

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
