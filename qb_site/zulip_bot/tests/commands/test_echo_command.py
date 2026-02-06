from __future__ import annotations

from django.test import SimpleTestCase

from zulip_bot.commands import CommandContext
from zulip_bot.commands.echo import echo_command


class TestEchoCommand(SimpleTestCase):
    def _context(self) -> CommandContext:
        return CommandContext(
            sender_id=1,
            sender_email="user@example.com",
            sender_full_name="User",
            message_content="echo",
            message_id=1,
            stream_id=5,
            topic="topic",
            is_private=False,
            allowed_command_names=frozenset({"echo"}),
        )

    def test_echo_repeats_text(self) -> None:
        result = echo_command(self._context(), "hello world")
        self.assertEqual(result.content, "hello world")

    def test_echo_uses_no_content_placeholder(self) -> None:
        result = echo_command(self._context(), "   ")
        self.assertEqual(result.content, "(no content)")
