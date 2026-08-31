"""The timezone chain behind every reviewer-facing local time (design docs 022, 050).

`away_until` is entered as a naive `datetime-local`, so what this resolves to decides what a
reviewer's break time actually means. Zulip's own user record is the authority; the fallbacks exist
for reviewers with no Zulip link (e.g. created by `import_reviewer_topics`).
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings

from core.models import User
from zulip_bot.services.user_timezone import is_valid_timezone_name, resolve_user_timezone_name
from zulip_bot.services.zulip_client import ZulipApiError


@override_settings(
    ZULIP_BASE_URL="https://zulip.example",
    ZULIP_BOT_EMAIL="bot@example.com",
    ZULIP_BOT_API_KEY="key",
    TIME_ZONE="UTC",
)
class ResolveUserTimezoneNameTests(TestCase):
    def test_prefers_the_zone_zulip_reports(self) -> None:
        user = User.objects.create(github_login="reviewer", zulip_user_id=101, timezone="America/New_York")
        with patch(
            "zulip_bot.services.user_timezone.ZulipClient.get_user_by_id",
            return_value={"user": {"timezone": "Europe/Berlin"}},
        ):
            self.assertEqual(resolve_user_timezone_name(user=user), "Europe/Berlin")

    def test_explicit_zulip_id_overrides_the_stored_link(self) -> None:
        # The caller may hold a verified id from elsewhere; it must win over the user's own field.
        user = User.objects.create(github_login="reviewer", zulip_user_id=101)
        with patch(
            "zulip_bot.services.user_timezone.ZulipClient.get_user_by_id",
            return_value={"user": {"timezone": "Europe/Berlin"}},
        ) as lookup:
            resolve_user_timezone_name(user=user, zulip_user_id=999)
        lookup.assert_called_once_with(999)

    def test_falls_back_to_the_stored_user_timezone(self) -> None:
        user = User.objects.create(github_login="reviewer", zulip_user_id=101, timezone="America/New_York")
        with patch("zulip_bot.services.user_timezone.ZulipClient.get_user_by_id", side_effect=ZulipApiError("boom")):
            self.assertEqual(resolve_user_timezone_name(user=user), "America/New_York")

    def test_falls_back_to_the_project_default(self) -> None:
        # No Zulip link at all — the importer-created reviewer case.
        user = User.objects.create(github_login="reviewer")
        self.assertEqual(resolve_user_timezone_name(user=user), "UTC")

    def test_ignores_an_invalid_zone_from_zulip(self) -> None:
        user = User.objects.create(github_login="reviewer", zulip_user_id=101, timezone="America/New_York")
        with patch(
            "zulip_bot.services.user_timezone.ZulipClient.get_user_by_id",
            return_value={"user": {"timezone": "Mars/Olympus_Mons"}},
        ):
            self.assertEqual(resolve_user_timezone_name(user=user), "America/New_York")

    def test_ignores_an_invalid_stored_zone(self) -> None:
        user = User.objects.create(github_login="reviewer", timezone="Mars/Olympus_Mons")
        self.assertEqual(resolve_user_timezone_name(user=user), "UTC")

    @override_settings(ZULIP_BASE_URL="", ZULIP_BOT_EMAIL="", ZULIP_BOT_API_KEY="")
    def test_unconfigured_zulip_is_not_an_error(self) -> None:
        user = User.objects.create(github_login="reviewer", zulip_user_id=101, timezone="America/New_York")
        self.assertEqual(resolve_user_timezone_name(user=user), "America/New_York")


class IsValidTimezoneNameTests(SimpleTestCase):
    def test_accepts_known_zones_and_rejects_junk(self) -> None:
        self.assertTrue(is_valid_timezone_name("Europe/Berlin"))
        self.assertFalse(is_valid_timezone_name("Mars/Olympus_Mons"))
