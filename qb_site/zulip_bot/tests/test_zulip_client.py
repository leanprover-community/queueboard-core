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

    def test_send_direct_message_json_encodes_recipients(self) -> None:
        with patch("zulip_bot.services.zulip_client.requests.request", return_value=self._response()) as mock_request:
            client = ZulipClient()
            client.send_direct_message(to=[101], content="hello")

        self.assertEqual(
            mock_request.call_args.kwargs["data"],
            {
                "type": "direct",
                "to": "[101]",
                "content": "hello",
            },
        )

    def test_send_stream_message_uses_stream_payload_shape(self) -> None:
        with patch("zulip_bot.services.zulip_client.requests.request", return_value=self._response()) as mock_request:
            client = ZulipClient()
            client.send_stream_message(stream=5, topic="topic", content="hello stream")

        self.assertEqual(
            mock_request.call_args.kwargs["data"],
            {
                "type": "stream",
                "to": 5,
                "topic": "topic",
                "content": "hello stream",
            },
        )

    def test_update_user_group_members_json_encodes_lists(self) -> None:
        with patch("zulip_bot.services.zulip_client.requests.request", return_value=self._response()) as mock_request:
            client = ZulipClient()
            client.update_user_group_members(user_group_id=123, add=[1, 2], delete=[3])

        self.assertEqual(
            mock_request.call_args.kwargs["data"],
            {
                "add": "[1, 2]",
                "delete": "[3]",
            },
        )

    def test_add_reaction_uses_reactions_endpoint(self) -> None:
        with patch("zulip_bot.services.zulip_client.requests.request", return_value=self._response()) as mock_request:
            client = ZulipClient()
            client.add_reaction(message_id=777, emoji_name="thumbs_up")

        self.assertTrue(mock_request.call_args.args[1].endswith("/api/v1/messages/777/reactions"))
        self.assertEqual(
            mock_request.call_args.kwargs["data"],
            {
                "message_id": 777,
                "emoji_name": "thumbs_up",
            },
        )

    def test_get_user_group_members_encodes_direct_member_only_as_json_bool(self) -> None:
        with patch("zulip_bot.services.zulip_client.requests.request", return_value=self._response()) as mock_request:
            client = ZulipClient()
            client.get_user_group_members(user_group_id=123, direct_member_only=True)

        self.assertEqual(mock_request.call_args.kwargs["params"], {"direct_member_only": "true"})

    @override_settings(ZULIP_USER_EMAIL="", ZULIP_USER_API_KEY="")
    def test_group_membership_requires_user_credentials_when_missing(self) -> None:
        with patch("zulip_bot.services.zulip_client.requests.request", return_value=self._response()) as mock_request:
            client = ZulipClient()
            with self.assertRaises(ZulipApiError) as ctx:
                client.is_user_group_member(user_group_id=123, user_id=456)

        self.assertIn("user credentials are required", str(ctx.exception).lower())
        mock_request.assert_not_called()
