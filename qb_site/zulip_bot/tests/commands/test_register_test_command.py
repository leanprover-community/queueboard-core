from __future__ import annotations

import re

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

    def test_register_test_returns_link(self) -> None:
        result = register_test_command(self._context(sender_id=101), "")
        self.assertIn("[test registration via GitHub OAuth](", result.content)
        self.assertIn("https://queueboard.example/api/zulip/register/", result.content)
        self.assertRegex(result.content, re.compile(r"<time:\d+>"))

    def test_register_test_handles_missing_sender_identity(self) -> None:
        result = register_test_command(self._context(sender_id=None), "")
        self.assertIn("Could not determine your Zulip identity", result.content)
