from __future__ import annotations

from django.test import SimpleTestCase, override_settings

from zulip_bot.commands import CommandContext
from zulip_bot.commands.console import console_command


class TestConsoleCommand(SimpleTestCase):
    def _context(self) -> CommandContext:
        return CommandContext(
            sender_id=101,
            sender_email="reviewer@example.com",
            sender_full_name="Reviewer User",
            message_content="console",
            message_id=1,
            stream_id=None,
            topic=None,
            is_private=True,
            allowed_command_names=frozenset({"console"}),
        )

    @override_settings(QUEUEBOARD_BASE_URL="https://queue.example.org")
    def test_returns_absolute_console_link(self) -> None:
        result = console_command(self._context(), "")
        # In-place reply (non-sensitive link), not a proactive DM.
        self.assertFalse(result.response_not_required)
        self.assertIn("[reviewer console](https://queue.example.org/console/)", result.content)

    @override_settings(QUEUEBOARD_BASE_URL="", ZULIP_PREFS_URL_BASE="https://legacy.example.org")
    def test_falls_back_to_legacy_base_url(self) -> None:
        result = console_command(self._context(), "")
        self.assertIn("https://legacy.example.org/console/", result.content)
