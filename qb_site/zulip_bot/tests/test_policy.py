from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from core.models import Repository, ReviewerPreference, User
from zulip_bot.tests.webhook_test_utils import WebhookTestMixin


@override_settings(
    ZULIP_WEBHOOK_TOKEN="test-token",
    ZULIP_BOT_EMAIL="qb-bot@example.com",
)
class TestZulipWebhookPolicy(WebhookTestMixin, TestCase):
    def assert_ignored(self, result: dict) -> None:
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["json"], {"response_not_required": True})

    def test_no_policy_defaults_to_denied(self) -> None:
        result = self._post_payload(self._payload(content="@**qb-bot** help", id=9, stream_id=5))
        self.assert_ignored(result)

    @override_settings(
        ZULIP_COMMAND_POLICY={
            "help": {"allowed_groups": ["all"], "allowed_contexts": ["stream:5"]},
            "echo": {"allowed_groups": ["all"], "allowed_contexts": ["stream:5"]},
        }
    )
    def test_help_command_lists_commands(self) -> None:
        result = self._post_payload(self._payload(content="@**qb-bot** help", id=10, stream_id=5))
        self.assertEqual(result["status"], 200)
        self.assertIn("- echo: Repeat the provided text.", result["json"]["content"])
        self.assertIn("- help: List supported commands.", result["json"]["content"])

    @override_settings(
        ZULIP_COMMAND_POLICY={
            "help": {"allowed_groups": ["all"], "allowed_contexts": ["stream:5"]},
            "echo": {"allowed_groups": ["all"], "allowed_contexts": ["stream:5"]},
        }
    )
    def test_echo_command_repeats_text(self) -> None:
        result = self._post_payload(self._payload(content="@**qb-bot** echo hello world", id=11, stream_id=5))
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["json"]["content"], "hello world")

    @override_settings(
        ZULIP_COMMAND_POLICY={
            "help": {"allowed_groups": ["all"], "allowed_contexts": ["stream:5"]},
        }
    )
    def test_missing_policy_entry_denies_command(self) -> None:
        result = self._post_payload(self._payload(content="@**qb-bot** echo hi", id=12, stream_id=5))
        self.assert_ignored(result)

    @override_settings(
        ZULIP_COMMAND_POLICY={
            "echo": {"allowed_contexts": []},
        }
    )
    def test_empty_allowed_contexts_means_denied(self) -> None:
        result = self._post_payload(self._payload(content="@**qb-bot** echo hi", id=16, stream_id=8))
        self.assert_ignored(result)

    @override_settings(
        ZULIP_COMMAND_POLICY={
            "echo": {"allowed_groups": ["all"], "allowed_contexts": ["all"]},
        }
    )
    def test_allowed_contexts_all_means_unrestricted(self) -> None:
        result = self._post_payload(self._payload(content="@**qb-bot** echo hi", id=17, stream_id=8))
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["json"]["content"], "hi")

    @override_settings(
        ZULIP_COMMAND_POLICY={
            "echo": {"allowed_groups": ["all"], "allowed_contexts": ["all"]},
        }
    )
    def test_allowed_groups_all_means_unrestricted(self) -> None:
        result = self._post_payload(self._payload(content="@**qb-bot** echo hi", id=18, stream_id=8))
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["json"]["content"], "hi")

    @override_settings(
        ZULIP_COMMAND_POLICY={
            "echo": {"allowed_user_ids": [101], "allowed_contexts": ["all"]},
        }
    )
    def test_allowed_user_ids_allows_specific_sender(self) -> None:
        result = self._post_payload(self._payload(content="@**qb-bot** echo hi", id=20, stream_id=8, sender_id=101))
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["json"]["content"], "hi")

    @override_settings(
        ZULIP_COMMAND_POLICY={
            "echo": {"allowed_groups": [1234], "allowed_user_ids": [101], "allowed_contexts": ["all"]},
        }
    )
    def test_allowed_user_ids_or_allowed_groups(self) -> None:
        result = self._post_payload(self._payload(content="@**qb-bot** echo hi", id=21, stream_id=8, sender_id=101))
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["json"]["content"], "hi")

    @override_settings(
        ZULIP_COMMAND_POLICY={
            "help": {"allowed_groups": ["all"], "allowed_contexts": ["dm"]},
            "echo": {"allowed_groups": ["all"], "allowed_contexts": ["dm"]},
        }
    )
    def test_disallowed_context_is_ignored(self) -> None:
        result = self._post_payload(self._payload(content="@**qb-bot** echo hi", id=13, stream_id=8))
        self.assert_ignored(result)

    @override_settings(
        ZULIP_COMMAND_POLICY={
            "help": {"allowed_groups": ["all"], "allowed_contexts": ["stream:5"]},
            "echo": {"allowed_groups": ["all"], "allowed_contexts": ["dm"]},
        }
    )
    def test_help_lists_only_allowed_commands(self) -> None:
        result = self._post_payload(self._payload(content="@**qb-bot** help", id=15, stream_id=5))
        self.assertEqual(result["status"], 200)
        self.assertNotIn("- echo:", result["json"]["content"])
        self.assertIn("- help:", result["json"]["content"])

    @override_settings(
        ZULIP_COMMAND_POLICY={
            "prefs": {"allowed_groups": ["all"], "allowed_contexts": ["dm"]},
        },
        QUEUEBOARD_BASE_URL="https://queueboard.example",
    )
    @patch("zulip_bot.commands.prefs.ZulipClient")
    def test_prefs_command_via_webhook(self, MockZulipClient: MagicMock) -> None:
        """prefs sends a DM with the link and returns response_not_required."""
        mock_client = MockZulipClient.return_value
        user = User.objects.create(github_login="reviewer", zulip_user_id=101)
        repo = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        ReviewerPreference.objects.create(user=user, repository=repo)

        result = self._post_payload(self._payload(content="prefs", id=19, message_type="private", sender_id=101))
        self.assert_ignored(result)
        mock_client.send_direct_message.assert_called_once()
        dm_content = mock_client.send_direct_message.call_args.kwargs["content"]
        self.assertIn("https://queueboard.example/console/preferences/", dm_content)

    @override_settings(
        ZULIP_COMMAND_POLICY={
            # Deployments (and our own runbook) spell this key with an underscore; it must keep
            # gating the command, whose canonical name is now hyphenated.
            "register_test": {"allowed_groups": ["all"], "allowed_contexts": ["dm"]},
        },
        QUEUEBOARD_BASE_URL="https://queueboard.example",
    )
    @patch("zulip_bot.commands.register_test.ZulipClient")
    def test_register_test_dispatches_from_either_spelling(self, MockZulipClient: MagicMock) -> None:
        # Regression: the command was registered as `register_test` while the parser hyphenates every
        # incoming name, so neither `register_test` nor `register-test` could ever reach it.
        for typed in ("register_test", "register-test"):
            with self.subTest(typed=typed):
                MockZulipClient.reset_mock()
                result = self._post_payload(
                    self._payload(content=typed, id=31, message_type="private", sender_id=101),
                )
                self.assert_ignored(result)  # the handler DMs and returns response_not_required
                dm_content = MockZulipClient.return_value.send_direct_message.call_args.kwargs["content"]
                self.assertIn("https://queueboard.example/api/zulip/register/", dm_content)

    @override_settings(
        ZULIP_COMMAND_POLICY={
            "register_test": {"allowed_groups": ["all"], "allowed_contexts": ["dm"]},
            "register-test": {"allowed_groups": ["all"], "allowed_contexts": ["dm"]},
        }
    )
    def test_duplicate_policy_spellings_are_logged(self) -> None:
        # The runtime cannot reject config, so it warns; `zulip_policy validate` is what refuses it.
        from zulip_bot.webhook.policy import _load_command_policy

        with self.assertLogs("zulip_bot.webhook.policy", level="WARNING") as logs:
            policy = _load_command_policy()

        self.assertIn("register-test", policy)
        self.assertEqual(len(policy), 1)
        self.assertTrue(any("zulip_command_policy_duplicate_entry" in line for line in logs.output))
