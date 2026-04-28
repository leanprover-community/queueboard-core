from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.test import TestCase

from core.models import User
from zulip_bot.commands import CommandContext
from zulip_bot.commands.label_pr import label_pr_command
from zulip_bot.services.close_pr_execution import PermissionCheckResult, PermissionOutcome


def _context(sender_id: int | None = 101, *, is_private: bool = True) -> CommandContext:
    return CommandContext(
        sender_id=sender_id,
        sender_email="reviewer@example.com",
        sender_full_name="Reviewer User",
        message_content="label-pr",
        message_id=555,
        stream_id=None if is_private else 5,
        topic=None if is_private else "topic",
        is_private=is_private,
        rendered_content=None,
        allowed_command_names=frozenset({"label-pr"}),
    )


_PR_URL = "https://github.com/leanprover-community/mathlib4/pull/999"
_ISSUE_URL = "https://github.com/leanprover-community/mathlib4/issues/777"


class TestLabelPRCommand(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = User.objects.create(zulip_user_id=101, github_login="reviewer")

    def _patch_permission(self, result: PermissionCheckResult):
        return patch(
            "zulip_bot.commands.label_pr.check_label_pr_permission",
            return_value=result,
        )

    def test_missing_sender_id(self) -> None:
        result = label_pr_command(_context(sender_id=None), _PR_URL)
        self.assertIn("Could not determine your Zulip identity", result.content)

    def test_no_pr_url(self) -> None:
        result = label_pr_command(_context(), "")
        self.assertIn("Could not parse `label-pr` command", result.content)

    def test_no_linked_user(self) -> None:
        self.user.delete()
        result = label_pr_command(_context(), _PR_URL)
        self.assertIn("No GitHub account is linked", result.content)

    def test_no_github_login(self) -> None:
        self.user.github_login = ""
        self.user.save()
        result = label_pr_command(_context(), _PR_URL)
        self.assertIn("does not have a GitHub login set", result.content)

    def test_token_unavailable(self) -> None:
        with self._patch_permission(PermissionCheckResult(outcome=PermissionOutcome.TOKEN_UNAVAILABLE)):
            result = label_pr_command(_context(), _PR_URL)
        self.assertIn("token for `label_pr` is not available", result.content)

    def test_github_error(self) -> None:
        with self._patch_permission(PermissionCheckResult(outcome=PermissionOutcome.GITHUB_ERROR)):
            result = label_pr_command(_context(), _PR_URL)
        self.assertIn("Could not fetch details", result.content)

    def test_pr_not_open(self) -> None:
        with self._patch_permission(PermissionCheckResult(outcome=PermissionOutcome.PR_NOT_OPEN, pr_title="Fix something")):
            result = label_pr_command(_context(), _PR_URL)
        self.assertIn("is not open", result.content)
        self.assertIn("Fix something", result.content)

    def test_not_permitted(self) -> None:
        with self._patch_permission(PermissionCheckResult(outcome=PermissionOutcome.NOT_PERMITTED)):
            result = label_pr_command(_context(), _PR_URL)
        self.assertIn("does not have permission", result.content)
        self.assertIn("to label", result.content)

    @patch("zulip_bot.commands.label_pr.ZulipClient")
    def test_permitted_sends_link_via_dm(self, MockZulipClient: MagicMock) -> None:
        """Label-editing link is delivered as a DM, not in the webhook response."""
        mock_client = MockZulipClient.return_value
        with self._patch_permission(PermissionCheckResult(outcome=PermissionOutcome.PERMITTED, pr_title="Great PR")):
            result = label_pr_command(_context(), _PR_URL)
        self.assertTrue(result.response_not_required)
        mock_client.send_direct_message.assert_called_once_with(
            to=[101], content=mock_client.send_direct_message.call_args.kwargs["content"]
        )
        dm_content = mock_client.send_direct_message.call_args.kwargs["content"]
        self.assertIn("edit labels on", dm_content)
        self.assertIn("Great PR", dm_content)
        self.assertIn("attributed to the bot", dm_content)
        self.assertIn("/api/zulip/label-pr/", dm_content)
        mock_client.add_reaction.assert_called_once_with(message_id=555, emoji_name="eyes")

    @patch("zulip_bot.commands.label_pr.ZulipClient")
    def test_permitted_link_includes_expiry(self, MockZulipClient: MagicMock) -> None:
        with self._patch_permission(PermissionCheckResult(outcome=PermissionOutcome.PERMITTED)):
            result = label_pr_command(_context(), _PR_URL)
        self.assertTrue(result.response_not_required)
        dm_content = MockZulipClient.return_value.send_direct_message.call_args.kwargs["content"]
        self.assertIn("<time:", dm_content)

    @patch("zulip_bot.commands.label_pr.ZulipClient")
    def test_permitted_with_issue_url(self, MockZulipClient: MagicMock) -> None:
        """label-pr accepts /issues/ URLs in addition to /pull/ URLs."""
        with self._patch_permission(PermissionCheckResult(outcome=PermissionOutcome.PERMITTED, pr_title="An issue")):
            result = label_pr_command(_context(), _ISSUE_URL)
        self.assertTrue(result.response_not_required)
        dm_content = MockZulipClient.return_value.send_direct_message.call_args.kwargs["content"]
        self.assertIn("edit labels on", dm_content)
        self.assertIn("mathlib4#777", dm_content)
        self.assertIn("/api/zulip/label-pr/", dm_content)

    @patch("zulip_bot.commands.label_pr.ZulipClient")
    def test_permitted_from_stream_sends_dm_not_stream_reply(self, MockZulipClient: MagicMock) -> None:
        """When invoked in a stream, the private link must still be sent via DM."""
        mock_client = MockZulipClient.return_value
        with self._patch_permission(PermissionCheckResult(outcome=PermissionOutcome.PERMITTED, pr_title="My PR")):
            result = label_pr_command(_context(is_private=False), _PR_URL)
        self.assertTrue(result.response_not_required)
        mock_client.send_direct_message.assert_called_once()
        dm_content = mock_client.send_direct_message.call_args.kwargs["content"]
        self.assertIn("edit labels on", dm_content)
