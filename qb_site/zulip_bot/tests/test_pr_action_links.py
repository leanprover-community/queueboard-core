from __future__ import annotations

from django.test import SimpleTestCase, override_settings

from zulip_bot.services.pr_action_links import (
    CLOSE_PR,
    LABEL_PR,
    PRActionLinkClaims,
    PRActionTokenExpired,
    PRActionTokenInvalid,
    build_pr_action_link,
    issue_pr_action_token,
    ttl_seconds,
    validate_pr_action_token,
)

NOW = 1_700_000_000


def _claims(**kwargs) -> PRActionLinkClaims:
    defaults = dict(
        zulip_user_id=101,
        github_login="reviewer",
        pr_owner="leanprover-community",
        pr_repo="mathlib4",
        pr_number=999,
    )
    defaults.update(kwargs)
    return PRActionLinkClaims(**defaults)


class TestPRActionTokenRoundTrip(SimpleTestCase):
    """Both actions share one implementation; run the same battery over each."""

    def test_round_trip(self) -> None:
        for action in (CLOSE_PR, LABEL_PR):
            with self.subTest(action=action.name):
                token = issue_pr_action_token(action=action, claims=_claims(), now=NOW)
                claims = validate_pr_action_token(token, action=action, now=NOW + 60)
                self.assertEqual(claims.zulip_user_id, 101)
                self.assertEqual(claims.github_login, "reviewer")
                self.assertEqual(claims.pr_owner, "leanprover-community")
                self.assertEqual(claims.pr_repo, "mathlib4")
                self.assertEqual(claims.pr_number, 999)
                self.assertEqual(claims.iat, NOW)
                self.assertEqual(claims.exp, NOW + ttl_seconds(action))

    def test_rejects_invalid_token(self) -> None:
        for action in (CLOSE_PR, LABEL_PR):
            with self.subTest(action=action.name):
                with self.assertRaises(PRActionTokenInvalid):
                    validate_pr_action_token("not-a-valid-token", action=action, now=NOW)

    def test_rejects_expired_token(self) -> None:
        for action in (CLOSE_PR, LABEL_PR):
            with self.subTest(action=action.name):
                token = issue_pr_action_token(action=action, claims=_claims(), now=NOW)
                with self.assertRaises(PRActionTokenExpired):
                    validate_pr_action_token(token, action=action, now=NOW + ttl_seconds(action) + 1)

    def test_rejects_tampered_token(self) -> None:
        for action in (CLOSE_PR, LABEL_PR):
            with self.subTest(action=action.name):
                token = issue_pr_action_token(action=action, claims=_claims(), now=NOW)
                mid = len(token) // 2
                flipped = token[:mid] + ("A" if token[mid] != "A" else "B") + token[mid + 1 :]
                with self.assertRaises(PRActionTokenInvalid):
                    validate_pr_action_token(flipped, action=action, now=NOW)


class TestPRActionTokensAreNotInterchangeable(SimpleTestCase):
    """The per-action salts are the reason one action's token cannot authorize the other.

    `close-pr` and `label-pr` grant different GitHub mutations, so this is a security property of the
    consolidation, not an incidental one: sharing a salt would have made a label token a close token.
    """

    def test_a_close_token_is_not_a_label_token(self) -> None:
        token = issue_pr_action_token(action=CLOSE_PR, claims=_claims(), now=NOW)
        with self.assertRaises(PRActionTokenInvalid):
            validate_pr_action_token(token, action=LABEL_PR, now=NOW)

    def test_a_label_token_is_not_a_close_token(self) -> None:
        token = issue_pr_action_token(action=LABEL_PR, claims=_claims(), now=NOW)
        with self.assertRaises(PRActionTokenInvalid):
            validate_pr_action_token(token, action=CLOSE_PR, now=NOW)

    @override_settings(ZULIP_CLOSE_PR_TOKEN_SECRET="close-secret", ZULIP_LABEL_PR_TOKEN_SECRET="label-secret")
    def test_separate_secrets_are_honored(self) -> None:
        token = issue_pr_action_token(action=CLOSE_PR, claims=_claims(), now=NOW)
        self.assertEqual(validate_pr_action_token(token, action=CLOSE_PR, now=NOW).pr_number, 999)
        with self.assertRaises(PRActionTokenInvalid):
            validate_pr_action_token(token, action=LABEL_PR, now=NOW)


class TestPRActionLinks(SimpleTestCase):
    @override_settings(QUEUEBOARD_BASE_URL="https://queueboard.example")
    def test_build_link_uses_url_base(self) -> None:
        self.assertTrue(
            build_pr_action_link(action=CLOSE_PR, claims=_claims()).startswith("https://queueboard.example/api/zulip/close-pr/")
        )
        self.assertTrue(
            build_pr_action_link(action=LABEL_PR, claims=_claims()).startswith("https://queueboard.example/api/zulip/label-pr/")
        )

    def test_build_link_falls_back_to_relative_path(self) -> None:
        self.assertTrue(build_pr_action_link(action=CLOSE_PR, claims=_claims()).startswith("/api/zulip/close-pr/"))
        self.assertTrue(build_pr_action_link(action=LABEL_PR, claims=_claims()).startswith("/api/zulip/label-pr/"))


class TestTTLSettings(SimpleTestCase):
    """The commands quote the expiry in their DM from `ttl_seconds`, so it must track the setting."""

    def test_default_ttl(self) -> None:
        self.assertEqual(ttl_seconds(CLOSE_PR), 1800)
        self.assertEqual(ttl_seconds(LABEL_PR), 1800)

    @override_settings(ZULIP_CLOSE_PR_TOKEN_TTL_SECONDS=60, ZULIP_LABEL_PR_TOKEN_TTL_SECONDS=120)
    def test_ttl_follows_settings_per_action(self) -> None:
        self.assertEqual(ttl_seconds(CLOSE_PR), 60)
        self.assertEqual(ttl_seconds(LABEL_PR), 120)
        close_token = issue_pr_action_token(action=CLOSE_PR, claims=_claims(), now=NOW)
        self.assertEqual(validate_pr_action_token(close_token, action=CLOSE_PR, now=NOW).exp, NOW + 60)
        label_token = issue_pr_action_token(action=LABEL_PR, claims=_claims(), now=NOW)
        self.assertEqual(validate_pr_action_token(label_token, action=LABEL_PR, now=NOW).exp, NOW + 120)


class TestClaimValidation(SimpleTestCase):
    def test_rejects_bad_claim_values(self) -> None:
        bad = [
            dict(zulip_user_id=0),
            dict(zulip_user_id=-1),
            dict(github_login="   "),
            dict(pr_owner=""),
            dict(pr_repo="  "),
            dict(pr_number=0),
            dict(pr_number=-5),
        ]
        for overrides in bad:
            with self.subTest(**overrides):
                token = issue_pr_action_token(action=CLOSE_PR, claims=_claims(**overrides), now=NOW)
                with self.assertRaises(PRActionTokenInvalid):
                    validate_pr_action_token(token, action=CLOSE_PR, now=NOW)

    def test_expiry_is_reported_before_claim_problems(self) -> None:
        """A token both expired and malformed reports as expired — `read_signed_payload` checks `exp` first."""
        token = issue_pr_action_token(action=CLOSE_PR, claims=_claims(pr_number=0), now=NOW)
        with self.assertRaises(PRActionTokenExpired):
            validate_pr_action_token(token, action=CLOSE_PR, now=NOW + ttl_seconds(CLOSE_PR) + 1)
