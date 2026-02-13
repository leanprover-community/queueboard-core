from __future__ import annotations

from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from zulip_bot.services.zulip_client import ZulipApiError, ZulipClient


@override_settings(
    ZULIP_BASE_URL="https://leanprover.zulipchat.com",
    ZULIP_BOT_EMAIL="bot@example.com",
    ZULIP_BOT_API_KEY="bot-key",
    ZULIP_USER_EMAIL="human@example.com",
    ZULIP_USER_API_KEY="human-key",
)
class TestZulipClient(SimpleTestCase):
    def _response(self) -> Mock:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"result": "success", "is_user_in_group": True}
        return response

    def test_group_membership_uses_user_credentials_when_configured(self) -> None:
        with patch("zulip_bot.services.zulip_client.requests.request", return_value=self._response()) as mock_request:
            client = ZulipClient()
            client.is_user_group_member(user_group_id=123, user_id=456)

        self.assertEqual(mock_request.call_args.kwargs["auth"], ("human@example.com", "human-key"))
        self.assertEqual(mock_request.call_args.kwargs["params"], {"direct_member_only": "false"})

    def test_get_user_groups_encodes_include_deactivated_as_json_bool(self) -> None:
        with patch("zulip_bot.services.zulip_client.requests.request", return_value=self._response()) as mock_request:
            client = ZulipClient()
            client.get_user_groups(include_deactivated=True)

        self.assertEqual(mock_request.call_args.kwargs["params"], {"include_deactivated_groups": "true"})

    @override_settings(ZULIP_USER_EMAIL="", ZULIP_USER_API_KEY="")
    def test_group_membership_requires_user_credentials_when_missing(self) -> None:
        with patch("zulip_bot.services.zulip_client.requests.request", return_value=self._response()) as mock_request:
            client = ZulipClient()
            with self.assertRaises(ZulipApiError) as ctx:
                client.is_user_group_member(user_group_id=123, user_id=456)

        self.assertIn("user credentials are required", str(ctx.exception).lower())
        mock_request.assert_not_called()
