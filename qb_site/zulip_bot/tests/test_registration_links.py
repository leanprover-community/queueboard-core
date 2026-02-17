from __future__ import annotations

from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from zulip_bot.services.registration_links import (
    RegistrationLinkClaims,
    RegistrationTokenExpired,
    RegistrationTokenInvalid,
    build_registration_link,
    issue_registration_token,
    validate_registration_token,
)


class TestRegistrationLinks(SimpleTestCase):
    @override_settings(ZULIP_PREFS_URL_BASE="https://queueboard.example")
    def test_build_registration_link_uses_prefs_base(self) -> None:
        link = build_registration_link(
            claims=RegistrationLinkClaims(
                zulip_user_id=101,
                sender_email="reviewer@example.com",
                sender_full_name="Reviewer User",
            )
        )
        self.assertTrue(link.startswith("https://queueboard.example/api/zulip/register/"))

    def test_issue_and_validate_registration_token_round_trip(self) -> None:
        token = issue_registration_token(
            claims=RegistrationLinkClaims(
                zulip_user_id=101,
                sender_email="reviewer@example.com",
                sender_full_name="Reviewer User",
                nonce="fixed-nonce",
            )
        )
        claims = validate_registration_token(token)
        self.assertEqual(claims.zulip_user_id, 101)
        self.assertEqual(claims.sender_email, "reviewer@example.com")
        self.assertEqual(claims.sender_full_name, "Reviewer User")
        self.assertEqual(claims.nonce, "fixed-nonce")
        self.assertIsNotNone(claims.iat)
        self.assertIsNotNone(claims.exp)

    def test_validate_registration_token_rejects_invalid_token(self) -> None:
        with self.assertRaises(RegistrationTokenInvalid):
            validate_registration_token("not-a-token")

    def test_validate_registration_token_rejects_expired_token(self) -> None:
        with patch("zulip_bot.services.registration_links.time.time", return_value=1_700_000_000):
            token = issue_registration_token(
                claims=RegistrationLinkClaims(
                    zulip_user_id=101,
                    sender_email="reviewer@example.com",
                )
            )
        with patch("zulip_bot.services.registration_links.time.time", return_value=1_700_000_000 + 1_900):
            with self.assertRaises(RegistrationTokenExpired):
                validate_registration_token(token)
