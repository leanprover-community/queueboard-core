from __future__ import annotations

from django.test import SimpleTestCase

from core.services.signed_payloads import (
    SignedPayloadExpired,
    SignedPayloadInvalid,
    issue_signed_payload,
    read_signed_payload,
)


class SignedPayloadTests(SimpleTestCase):
    def test_round_trip(self) -> None:
        value = issue_signed_payload({"k": "v"}, secret="s", salt="salt", ttl_seconds=600, now=1000)
        payload = read_signed_payload(value, secret="s", salt="salt", now=1100)
        self.assertEqual(payload["k"], "v")
        self.assertEqual(payload["iat"], 1000)
        self.assertEqual(payload["exp"], 1600)

    def test_expired(self) -> None:
        value = issue_signed_payload({"k": "v"}, secret="s", salt="salt", ttl_seconds=600, now=1000)
        with self.assertRaises(SignedPayloadExpired):
            read_signed_payload(value, secret="s", salt="salt", now=1601)

    def test_wrong_secret_is_invalid(self) -> None:
        value = issue_signed_payload({"k": "v"}, secret="s", salt="salt", ttl_seconds=600, now=1000)
        with self.assertRaises(SignedPayloadInvalid):
            read_signed_payload(value, secret="other", salt="salt", now=1100)

    def test_tampered_is_invalid(self) -> None:
        with self.assertRaises(SignedPayloadInvalid):
            read_signed_payload("not-a-real-token", secret="s", salt="salt", now=1100)
