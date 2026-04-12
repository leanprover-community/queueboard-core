from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from core.models import User
from zulip_bot.commands import CommandContext
from zulip_bot.commands.close_pr import close_pr_command
from zulip_bot.services.close_pr_execution import PermissionCheckResult, PermissionOutcome


def _context(sender_id: int | None = 101) -> CommandContext:
    return CommandContext(
        sender_id=sender_id,
        sender_email="reviewer@example.com",
        sender_full_name="Reviewer User",
        message_content="close-pr",
        message_id=555,
        stream_id=None,
        topic=None,
        is_private=True,
        rendered_content=None,
        allowed_command_names=frozenset({"close-pr"}),
    )


_PR_URL = "https://github.com/leanprover-community/mathlib4/pull/999"


class TestClosePRCommand(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = User.objects.create(zulip_user_id=101, github_login="reviewer")

    def _patch_permission(self, result: PermissionCheckResult):
        return patch(
            "zulip_bot.commands.close_pr.check_close_pr_permission",
            return_value=result,
        )

    def test_missing_sender_id(self) -> None:
        result = close_pr_command(_context(sender_id=None), _PR_URL)
        self.assertIn("Could not determine your Zulip identity", result.content)

    def test_no_pr_url(self) -> None:
        result = close_pr_command(_context(), "")
        self.assertIn("Could not parse `close-pr` command", result.content)

    def test_no_linked_user(self) -> None:
        self.user.delete()
        result = close_pr_command(_context(), _PR_URL)
        self.assertIn("No GitHub account is linked", result.content)

    def test_no_github_login(self) -> None:
        self.user.github_login = ""
        self.user.save()
        result = close_pr_command(_context(), _PR_URL)
        self.assertIn("does not have a GitHub login set", result.content)

    def test_token_unavailable(self) -> None:
        with self._patch_permission(PermissionCheckResult(outcome=PermissionOutcome.TOKEN_UNAVAILABLE)):
            result = close_pr_command(_context(), _PR_URL)
        self.assertIn("token for `close_pr` is not available", result.content)

    def test_github_error(self) -> None:
        with self._patch_permission(PermissionCheckResult(outcome=PermissionOutcome.GITHUB_ERROR)):
            result = close_pr_command(_context(), _PR_URL)
        self.assertIn("Could not fetch PR details", result.content)

    def test_pr_not_open(self) -> None:
        with self._patch_permission(PermissionCheckResult(outcome=PermissionOutcome.PR_NOT_OPEN, pr_title="Fix something")):
            result = close_pr_command(_context(), _PR_URL)
        self.assertIn("is not open", result.content)
        self.assertIn("Fix something", result.content)

    def test_not_permitted(self) -> None:
        with self._patch_permission(PermissionCheckResult(outcome=PermissionOutcome.NOT_PERMITTED)):
            result = close_pr_command(_context(), _PR_URL)
        self.assertIn("does not have permission to close", result.content)

    def test_permitted_issues_link(self) -> None:
        with self._patch_permission(PermissionCheckResult(outcome=PermissionOutcome.PERMITTED, pr_title="Great PR")):
            result = close_pr_command(_context(), _PR_URL)
        self.assertIn("confirm closing PR", result.content)
        self.assertIn("Great PR", result.content)
        self.assertIn("attributed to the bot", result.content)
        self.assertIn("/api/zulip/close-pr/", result.content)

    def test_permitted_link_includes_expiry(self) -> None:
        with self._patch_permission(PermissionCheckResult(outcome=PermissionOutcome.PERMITTED)):
            result = close_pr_command(_context(), _PR_URL)
        self.assertIn("<time:", result.content)
