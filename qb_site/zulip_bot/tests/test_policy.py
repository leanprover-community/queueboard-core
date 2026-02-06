from __future__ import annotations

from django.test import TestCase, override_settings

from zulip_bot.tests.webhook_test_utils import WebhookTestMixin


@override_settings(ZULIP_WEBHOOK_TOKEN="test-token")
class TestZulipWebhookPolicy(WebhookTestMixin, TestCase):
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
