from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from core.models import Repository, ReviewerPreference, User
from zulip_bot.commands import CommandContext
from zulip_bot.commands.prefs import prefs_command


@override_settings(QUEUEBOARD_BASE_URL="https://queueboard.example")
class TestPrefsCommand(TestCase):
    def _context(self, *, sender_id: int) -> CommandContext:
        return CommandContext(
            sender_id=sender_id,
            sender_email="reviewer@example.com",
            sender_full_name="Reviewer User",
            message_content="prefs",
            message_id=1,
            stream_id=None,
            topic=None,
            is_private=True,
            allowed_command_names=frozenset({"prefs"}),
        )

    @patch("zulip_bot.commands.prefs.ZulipClient")
    def test_prefs_command_returns_link_for_reviewer(self, MockZulipClient: MagicMock) -> None:
        mock_client = MockZulipClient.return_value
        user = User.objects.create(github_login="reviewer", zulip_user_id=101)
        repo = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        ReviewerPreference.objects.create(user=user, repository=repo)

        result = prefs_command(self._context(sender_id=101), "")
        self.assertTrue(result.response_not_required)
        mock_client.send_direct_message.assert_called_once()
        dm_content = mock_client.send_direct_message.call_args.kwargs["content"]
        self.assertIn("[open your reviewer preferences form](", dm_content)
        self.assertIn("https://queueboard.example/api/zulip/prefs/", dm_content)
        self.assertRegex(dm_content, re.compile(r"<time:\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00>"))

    @patch("zulip_bot.commands.prefs.ZulipClient")
    def test_prefs_command_handles_missing_user_link(self, MockZulipClient: MagicMock) -> None:
        mock_client = MockZulipClient.return_value
        result = prefs_command(self._context(sender_id=101), "")
        self.assertTrue(result.response_not_required)
        mock_client.send_direct_message.assert_called_once()
        dm_content = mock_client.send_direct_message.call_args.kwargs["content"]
        self.assertIn("No reviewer profile is linked", dm_content)
        self.assertIn("[start registration](", dm_content)
        self.assertIn("https://queueboard.example/api/zulip/register/", dm_content)
        self.assertRegex(dm_content, re.compile(r"<time:\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00>"))

    def test_prefs_command_handles_missing_preferences(self) -> None:
        User.objects.create(github_login="reviewer", zulip_user_id=101)

        result = prefs_command(self._context(sender_id=101), "")
        self.assertIn("do not currently have any reviewer preferences", result.content)
