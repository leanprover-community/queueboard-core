from __future__ import annotations

from unittest.mock import patch

from django.test import SimpleTestCase

from zulip_bot.services.zulip_client import ZulipApiError
from zulip_bot.webhook.membership import GroupMembershipCheckError, GroupMembershipChecker


class TestGroupMembershipChecker(SimpleTestCase):
    def test_bot_restricted_endpoint_raises_group_check_error(self) -> None:
        checker = GroupMembershipChecker()

        with (
            patch("zulip_bot.webhook.membership.ZulipClient") as mock_client_cls,
            patch("zulip_bot.webhook.membership.logger.exception"),
        ):
            mock_client = mock_client_cls.return_value
            mock_client.is_user_group_member.side_effect = ZulipApiError(
                "Zulip API request failed",
                payload={
                    "result": "error",
                    "code": "BAD_REQUEST",
                    "msg": "This endpoint does not accept bot requests.",
                },
            )
            with self.assertRaises(GroupMembershipCheckError) as ctx:
                checker.is_member_any(user_id=123, group_ids=frozenset({507749}))

        self.assertIn("group membership check failed", str(ctx.exception).lower())
        mock_client.get_user_group_members.assert_not_called()

    def test_non_bot_error_also_raises_group_check_error(self) -> None:
        checker = GroupMembershipChecker()

        with (
            patch("zulip_bot.webhook.membership.ZulipClient") as mock_client_cls,
            patch("zulip_bot.webhook.membership.logger.exception"),
        ):
            mock_client = mock_client_cls.return_value
            mock_client.is_user_group_member.side_effect = ZulipApiError(
                "Zulip API request failed",
                payload={"result": "error", "msg": "Some other failure"},
            )
            with self.assertRaises(GroupMembershipCheckError):
                checker.is_member_any(user_id=123, group_ids=frozenset({507749}))

        mock_client.get_user_group_members.assert_not_called()
