from __future__ import annotations

from django.test import SimpleTestCase, override_settings

from zulip_bot.webhook.payload import (
    has_leading_bot_mention,
    parse_command,
    strip_leading_bot_mention,
    validate_payload,
)


class TestPayloadParser(SimpleTestCase):
    def test_parse_command_does_not_strip_mention_prefix(self) -> None:
        parsed = parse_command("@**queueboard-bot** echo hello world")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.name, "@**queueboard-bot**")
        self.assertEqual(parsed.args, "echo hello world")

    def test_parse_command_with_slash_prefix(self) -> None:
        parsed = parse_command("/help")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.name, "help")
        self.assertEqual(parsed.args, "")

    def test_parse_command_normalizes_underscores_to_hyphens(self) -> None:
        parsed = parse_command("close_pr https://github.com/org/repo/pull/1")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.name, "close-pr")

    def test_parse_command_normalizes_underscores_preserves_args(self) -> None:
        parsed = parse_command("assigned_prs")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.name, "assigned-prs")
        self.assertEqual(parsed.args, "")

    def test_parse_command_returns_none_when_only_mention(self) -> None:
        self.assertIsNone(parse_command("@**queueboard-bot**"))

    @override_settings(ZULIP_BOT_EMAIL="qb-bot@example.com")
    def test_has_leading_bot_mention_matches_configured_bot(self) -> None:
        payload = {"bot_email": "qb-bot@example.com", "message": {}}
        self.assertTrue(has_leading_bot_mention("@**qb-bot** help", payload))

    @override_settings(ZULIP_BOT_EMAIL="qb-bot@example.com")
    def test_has_leading_bot_mention_rejects_silent_mention(self) -> None:
        payload = {"bot_email": "qb-bot@example.com", "message": {}}
        self.assertFalse(has_leading_bot_mention("@_**qb-bot** help", payload))

    @override_settings(ZULIP_BOT_EMAIL="qb-bot@example.com")
    def test_has_leading_bot_mention_rejects_nonleading_mention(self) -> None:
        payload = {"bot_email": "qb-bot@example.com", "message": {}}
        self.assertFalse(has_leading_bot_mention("Announcing @**qb-bot**: help", payload))

    @override_settings(ZULIP_BOT_EMAIL="qb-bot@example.com")
    def test_has_leading_bot_mention_rejects_other_user(self) -> None:
        payload = {"bot_email": "qb-bot@example.com", "message": {}}
        self.assertFalse(has_leading_bot_mention("@**other-user** @**qb-bot** help", payload))

    @override_settings(ZULIP_BOT_EMAIL="qb-bot@example.com")
    def test_strip_leading_bot_mention_strips_only_bot(self) -> None:
        payload = {"bot_email": "qb-bot@example.com", "message": {}}
        stripped = strip_leading_bot_mention("@**qb-bot** @**alice** help", payload)
        self.assertEqual(stripped, "@**alice** help")

    @override_settings(ZULIP_BOT_EMAIL="qb-bot@example.com")
    def test_strip_leading_bot_silent_mention_is_not_stripped(self) -> None:
        payload = {"bot_email": "qb-bot@example.com", "message": {}}
        stripped = strip_leading_bot_mention("@_**qb-bot** @**alice** help", payload)
        self.assertEqual(stripped, "@_**qb-bot** @**alice** help")

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
