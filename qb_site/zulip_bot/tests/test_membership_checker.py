from __future__ import annotations

from unittest.mock import patch

from django.test import SimpleTestCase

from zulip_bot.services.zulip_client import ZulipApiError
from zulip_bot.webhook.membership import GroupMembershipChecker


class TestGroupMembershipChecker(SimpleTestCase):
    def test_falls_back_to_member_list_when_direct_endpoint_rejects_bot(self) -> None:
        checker = GroupMembershipChecker()

        with patch("zulip_bot.webhook.membership.ZulipClient") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.is_user_group_member.side_effect = ZulipApiError(
                "Zulip API request failed",
                payload={
                    "result": "error",
                    "code": "BAD_REQUEST",
                    "msg": "This endpoint does not accept bot requests.",
                },
            )
            mock_client.get_user_group_members.return_value = {"result": "success", "members": [123, 456]}

            allowed = checker.is_member_any(user_id=123, group_ids=frozenset({507749}))

        self.assertTrue(allowed)
        mock_client.get_user_group_members.assert_called_once_with(user_group_id=507749)

    def test_non_bot_error_remains_denied(self) -> None:
        checker = GroupMembershipChecker()

        with patch("zulip_bot.webhook.membership.ZulipClient") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.is_user_group_member.side_effect = ZulipApiError(
                "Zulip API request failed",
                payload={"result": "error", "msg": "Some other failure"},
            )

            allowed = checker.is_member_any(user_id=123, group_ids=frozenset({507749}))

        self.assertFalse(allowed)
        mock_client.get_user_group_members.assert_not_called()
