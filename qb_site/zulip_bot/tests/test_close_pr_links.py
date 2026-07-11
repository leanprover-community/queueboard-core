from __future__ import annotations

from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from zulip_bot.services.close_pr_links import (
    ClosePRLinkClaims,
    ClosePRTokenExpired,
    ClosePRTokenInvalid,
    build_close_pr_link,
    issue_close_pr_token,
    validate_close_pr_token,
)


class TestClosePRLinks(SimpleTestCase):
    def _claims(self, **kwargs) -> ClosePRLinkClaims:
        defaults = dict(
            zulip_user_id=101,
            github_login="reviewer",
            pr_owner="leanprover-community",
            pr_repo="mathlib4",
            pr_number=999,
        )
        defaults.update(kwargs)
        return ClosePRLinkClaims(**defaults)

    def test_round_trip(self) -> None:
        token = issue_close_pr_token(claims=self._claims())
        claims = validate_close_pr_token(token)
        self.assertEqual(claims.zulip_user_id, 101)
        self.assertEqual(claims.github_login, "reviewer")
        self.assertEqual(claims.pr_owner, "leanprover-community")
        self.assertEqual(claims.pr_repo, "mathlib4")
        self.assertEqual(claims.pr_number, 999)
        self.assertIsNotNone(claims.iat)
        self.assertIsNotNone(claims.exp)

    @override_settings(QUEUEBOARD_BASE_URL="https://queueboard.example")
    def test_build_link_uses_url_base(self) -> None:
        link = build_close_pr_link(claims=self._claims())
        self.assertTrue(link.startswith("https://queueboard.example/api/zulip/close-pr/"))

    def test_build_link_falls_back_to_relative_path(self) -> None:
        link = build_close_pr_link(claims=self._claims())
        self.assertTrue(link.startswith("/api/zulip/close-pr/"))

    def test_rejects_invalid_token(self) -> None:
        with self.assertRaises(ClosePRTokenInvalid):
            validate_close_pr_token("not-a-valid-token")

    def test_rejects_expired_token(self) -> None:
        with patch("zulip_bot.services.close_pr_links.time.time", return_value=1_700_000_000):
            token = issue_close_pr_token(claims=self._claims())
        with patch("zulip_bot.services.close_pr_links.time.time", return_value=1_700_000_000 + 1_900):
            with self.assertRaises(ClosePRTokenExpired):
                validate_close_pr_token(token)

    def test_rejects_tampered_token(self) -> None:
        token = issue_close_pr_token(claims=self._claims())
        # Flip a character in the middle.
        mid = len(token) // 2
        flipped = token[:mid] + ("A" if token[mid] != "A" else "B") + token[mid + 1 :]
        with self.assertRaises(ClosePRTokenInvalid):
            validate_close_pr_token(flipped)
