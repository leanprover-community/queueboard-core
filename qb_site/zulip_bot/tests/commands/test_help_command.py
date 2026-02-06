from __future__ import annotations

from django.test import SimpleTestCase

from zulip_bot.commands import CommandContext
from zulip_bot.commands.help import help_command


class TestHelpCommand(SimpleTestCase):
    def _context(self, *, allowed_names: frozenset[str]) -> CommandContext:
        return CommandContext(
            sender_id=1,
            sender_email="user@example.com",
            sender_full_name="User",
            message_content="help",
            message_id=1,
            stream_id=5,
            topic="topic",
            is_private=False,
            allowed_command_names=allowed_names,
        )

    def test_help_lists_only_allowed_commands(self) -> None:
        result = help_command(self._context(allowed_names=frozenset({"help"})), "")
        self.assertIn("- help: List supported commands.", result.content)
        self.assertNotIn("- echo: Repeat the provided text.", result.content)

    def test_help_handles_no_available_commands(self) -> None:
        result = help_command(self._context(allowed_names=frozenset({"not-a-command"})), "")
        self.assertIn("- (no commands available in this context)", result.content)
