from __future__ import annotations

from django.test import SimpleTestCase

from zulip_bot.services.registration_oauth_state import (
    RegistrationOAuthStateClaims,
    RegistrationOAuthStateExpired,
    RegistrationOAuthStateInvalid,
    issue_registration_oauth_state,
    validate_registration_oauth_state,
)


class TestRegistrationOAuthState(SimpleTestCase):
    def test_issue_and_validate_round_trip(self) -> None:
        state = issue_registration_oauth_state(
            claims=RegistrationOAuthStateClaims(
                registration_token="reg-token",
                registration_nonce="nonce-123",
            )
        )
        claims = validate_registration_oauth_state(state)
        self.assertEqual(claims.registration_token, "reg-token")
        self.assertEqual(claims.registration_nonce, "nonce-123")
        self.assertIsNotNone(claims.iat)
        self.assertIsNotNone(claims.exp)

    def test_invalid_state_rejected(self) -> None:
        with self.assertRaises(RegistrationOAuthStateInvalid):
            validate_registration_oauth_state("not-a-state")

    def test_expired_state_rejected(self) -> None:
        state = issue_registration_oauth_state(
            claims=RegistrationOAuthStateClaims(
                registration_token="reg-token",
                registration_nonce="nonce-123",
            ),
            now=1_700_000_000,
        )
        with self.assertRaises(RegistrationOAuthStateExpired):
            validate_registration_oauth_state(state, now=1_700_000_000 + 900)
