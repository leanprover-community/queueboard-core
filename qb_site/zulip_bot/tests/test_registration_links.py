from __future__ import annotations

from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings

from zulip_bot.services.registration_links import (
    RegistrationLinkClaims,
    RegistrationTokenExpired,
    RegistrationTokenInvalid,
    build_registration_link,
    issue_registration_token,
    validate_registration_token,
)


class TestRegistrationLinks(SimpleTestCase):
    @override_settings(QUEUEBOARD_BASE_URL="https://queueboard.example")
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


@override_settings(ZULIP_LINK_TOKEN_SECRET="shared-link-secret")
class TestRegistrationTokenSecret(TestCase):
    """Registration tokens and their OAuth state share one signing secret.

    It used to be `ZULIP_PREFS_TOKEN_SECRET`, owned by the retired prefs links (design doc 022). The
    setting is now `ZULIP_LINK_TOKEN_SECRET`; `base.py` still reads the old *env* name so a deployment
    that set it keeps the same key (a rename that silently fell back to SECRET_KEY would invalidate
    every in-flight registration link).
    """

    def test_registration_token_round_trips_under_the_shared_secret(self) -> None:
        claims = RegistrationLinkClaims(
            zulip_user_id=101,
            sender_email="reviewer@example.com",
            sender_full_name="Reviewer User",
            nonce="nonce-123",
        )
        token = issue_registration_token(claims=claims)
        self.assertEqual(validate_registration_token(token).zulip_user_id, 101)

    def test_a_token_signed_under_another_secret_is_rejected(self) -> None:
        with override_settings(ZULIP_LINK_TOKEN_SECRET="other-secret"):
            token = issue_registration_token(
                claims=RegistrationLinkClaims(
                    zulip_user_id=101,
                    sender_email="reviewer@example.com",
                    sender_full_name="Reviewer User",
                    nonce="nonce-123",
                )
            )
        with self.assertRaises(RegistrationTokenInvalid):
            validate_registration_token(token)
