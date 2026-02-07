from __future__ import annotations

from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from zulip_bot.services.zulip_client import ZulipClient


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

    @override_settings(ZULIP_USER_EMAIL="", ZULIP_USER_API_KEY="")
    def test_group_membership_falls_back_to_bot_credentials_when_user_credentials_missing(self) -> None:
        with patch("zulip_bot.services.zulip_client.requests.request", return_value=self._response()) as mock_request:
            client = ZulipClient()
            client.is_user_group_member(user_group_id=123, user_id=456)

        self.assertEqual(mock_request.call_args.kwargs["auth"], ("bot@example.com", "bot-key"))
