from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from zulip_bot.commands import CommandContext
from zulip_bot.commands.register_test import register_test_command


@override_settings(ZULIP_PREFS_URL_BASE="https://queueboard.example")
class TestRegisterTestCommand(SimpleTestCase):
    def _context(self, *, sender_id: int | None) -> CommandContext:
        return CommandContext(
            sender_id=sender_id,
            sender_email="reviewer@example.com",
            sender_full_name="Reviewer User",
            message_content="register_test",
            message_id=1,
            stream_id=None,
            topic=None,
            is_private=True,
            allowed_command_names=frozenset({"register_test"}),
        )

    @patch("zulip_bot.commands.register_test.ZulipClient")
    def test_register_test_returns_link(self, MockZulipClient: MagicMock) -> None:
        mock_client = MockZulipClient.return_value
        result = register_test_command(self._context(sender_id=101), "")
        self.assertTrue(result.response_not_required)
        mock_client.send_direct_message.assert_called_once()
        dm_content = mock_client.send_direct_message.call_args.kwargs["content"]
        self.assertIn("[test registration via GitHub OAuth](", dm_content)
        self.assertIn("https://queueboard.example/api/zulip/register/", dm_content)
        self.assertRegex(dm_content, re.compile(r"<time:\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00>"))

    def test_register_test_handles_missing_sender_identity(self) -> None:
        result = register_test_command(self._context(sender_id=None), "")
        self.assertIn("Could not determine your Zulip identity", result.content)
