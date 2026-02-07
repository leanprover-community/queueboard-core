from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from zulip_bot.services.prefs_links import PrefsLinkClaims, issue_prefs_token


class TestPrefsForm(TestCase):
    def _token(self) -> str:
        return issue_prefs_token(
            claims=PrefsLinkClaims(
                user_id=11,
                zulip_user_id=101,
                preference_ids=(31, 32),
            )
        )

    def test_get_with_valid_token_renders_dummy_form(self) -> None:
        token = self._token()
        response = self.client.get(reverse("zulip-prefs-form", kwargs={"token": token}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Reviewer preferences (dummy form)")
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_post_with_valid_token_shows_success_message(self) -> None:
        token = self._token()
        response = self.client.post(
            reverse("zulip-prefs-form", kwargs={"token": token}),
            data={"notes": "hello"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "submitted successfully")

    def test_invalid_token_returns_forbidden(self) -> None:
        response = self.client.get(reverse("zulip-prefs-form", kwargs={"token": "not-a-token"}))

        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "invalid", status_code=403)

    def test_expired_token_returns_forbidden(self) -> None:
        with patch("zulip_bot.services.prefs_links.time.time", return_value=1_700_000_000):
            token = issue_prefs_token(
                claims=PrefsLinkClaims(
                    user_id=11,
                    zulip_user_id=101,
                    preference_ids=(31, 32),
                )
            )
        with patch("zulip_bot.services.prefs_links.time.time", return_value=1_700_000_000 + 1_900):
            response = self.client.get(reverse("zulip-prefs-form", kwargs={"token": token}))

        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "expired", status_code=403)
