from __future__ import annotations

from unittest.mock import patch

from django.test import SimpleTestCase

from zulip_bot.webhook.sender import SenderClassifier


class TestSenderClassifier(SimpleTestCase):
    def _payload(self, sender_id: int) -> dict:
        return {
            "message": {
                "id": 1,
                "type": "stream",
                "content": "echo hi",
                "sender_id": sender_id,
                "sender_email": "user@example.com",
                "sender_full_name": "User",
                "stream_id": 5,
                "subject": "topic",
            }
        }

    def test_bot_user_via_lookup(self) -> None:
        classifier = SenderClassifier()
        with patch("zulip_bot.webhook.sender.ZulipClient") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.get_user_by_id.return_value = {"user": {"is_bot": True}}
            self.assertTrue(classifier.is_bot_sender(self._payload(42)))

    def test_human_user_via_lookup(self) -> None:
        classifier = SenderClassifier()
        with patch("zulip_bot.webhook.sender.ZulipClient") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.get_user_by_id.return_value = {"user": {"is_bot": False}}
            self.assertFalse(classifier.is_bot_sender(self._payload(7)))

    def test_missing_sender_id_returns_false_without_lookup(self) -> None:
        classifier = SenderClassifier()
        with patch("zulip_bot.webhook.sender.ZulipClient") as mock_client_cls:
            payload = self._payload(1)
            del payload["message"]["sender_id"]
            self.assertFalse(classifier.is_bot_sender(payload))
            mock_client_cls.assert_not_called()

    def test_lookup_result_is_cached(self) -> None:
        classifier = SenderClassifier()
        with patch("zulip_bot.webhook.sender.ZulipClient") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.get_user_by_id.return_value = {"user": {"is_bot": False}}
            self.assertFalse(classifier.is_bot_sender(self._payload(88)))
            self.assertFalse(classifier.is_bot_sender(self._payload(88)))
            mock_client.get_user_by_id.assert_called_once_with(88)

    def test_ignores_undocumented_root_is_bot_shape(self) -> None:
        classifier = SenderClassifier()
        with patch("zulip_bot.webhook.sender.ZulipClient") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.get_user_by_id.return_value = {"is_bot": True}
            self.assertFalse(classifier.is_bot_sender(self._payload(91)))

    def test_ignores_undocumented_user_type_shape(self) -> None:
        classifier = SenderClassifier()
        with patch("zulip_bot.webhook.sender.ZulipClient") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.get_user_by_id.return_value = {"user": {"user_type": "bot"}}
            self.assertFalse(classifier.is_bot_sender(self._payload(92)))
