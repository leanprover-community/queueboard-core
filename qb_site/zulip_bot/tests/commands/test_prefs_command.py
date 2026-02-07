from __future__ import annotations

from django.test import TestCase, override_settings

from core.models import Repository, ReviewerPreference, User
from zulip_bot.commands import CommandContext
from zulip_bot.commands.prefs import prefs_command


@override_settings(ZULIP_PREFS_URL_BASE="https://queueboard.example")
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

    def test_prefs_command_returns_link_for_reviewer(self) -> None:
        user = User.objects.create(github_login="reviewer", zulip_user_id=101)
        repo = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        ReviewerPreference.objects.create(user=user, repository=repo)

        result = prefs_command(self._context(sender_id=101), "")
        self.assertIn("expires in about 30 minutes", result.content)
        self.assertIn("https://queueboard.example/api/zulip/prefs/", result.content)

    def test_prefs_command_handles_missing_user_link(self) -> None:
        result = prefs_command(self._context(sender_id=101), "")
        self.assertIn("No reviewer profile is linked", result.content)

    def test_prefs_command_handles_missing_preferences(self) -> None:
        User.objects.create(github_login="reviewer", zulip_user_id=101)

        result = prefs_command(self._context(sender_id=101), "")
        self.assertIn("do not currently have any reviewer preferences", result.content)
