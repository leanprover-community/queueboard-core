from __future__ import annotations

from django.test import SimpleTestCase

from zulip_bot.webhook.payload import parse_command, validate_payload


class TestPayloadParser(SimpleTestCase):
    def test_parse_command_strips_mention_prefix(self) -> None:
        parsed = parse_command("@**queueboard-bot** echo hello world")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.name, "echo")
        self.assertEqual(parsed.args, "hello world")

    def test_parse_command_with_mention_colon_prefix(self) -> None:
        parsed = parse_command("@**queueboard-bot**: /help")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.name, "help")
        self.assertEqual(parsed.args, "")

    def test_parse_command_returns_none_when_only_mention(self) -> None:
        self.assertIsNone(parse_command("@**queueboard-bot**"))

    def test_validate_payload_requires_sender_fields(self) -> None:
        payload = {
            "message": {
                "id": 1,
                "type": "private",
                "content": "help",
            }
        }
        errors = validate_payload(payload)
        self.assertIn("missing_or_invalid_field:message.sender_id", errors)
        self.assertIn("missing_or_invalid_field:message.sender_email", errors)
        self.assertIn("missing_or_invalid_field:message.sender_full_name", errors)

    def test_validate_payload_stream_requires_stream_id(self) -> None:
        payload = {
            "message": {
                "id": 1,
                "type": "stream",
                "content": "help",
                "sender_id": 10,
                "sender_email": "x@example.com",
                "sender_full_name": "X",
            }
        }
        errors = validate_payload(payload)
        self.assertIn("missing_or_invalid_field:message.stream_id", errors)
