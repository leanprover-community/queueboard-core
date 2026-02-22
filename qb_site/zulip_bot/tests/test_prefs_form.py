from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import Repository, ReviewerPreference, User
from core.services.reviewer_notification_settings import (
    DEFAULT_AUTO_UNASSIGN_DAYS,
    DEFAULT_STALE_NUDGE_DAYS,
)
from syncer.models import LabelDef
from zulip_bot.forms import reviewer_preference_unaccounted_fields
from zulip_bot.services.prefs_links import PrefsLinkClaims, issue_prefs_token


class TestPrefsForm(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = User.objects.create(github_login="reviewer", zulip_user_id=101)
        self.repo1 = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        self.repo2 = Repository.objects.create(owner="leanprover", name="stdlib", default_branch="master")
        self.pref1 = ReviewerPreference.objects.create(
            user=self.user,
            repository=self.repo1,
            maximum_capacity=10,
            auto_assign=True,
            notifications_enabled=False,
            notification_settings={"stale_nudge_days": 2, "auto_unassign_days": 5},
            preferred_labels=["t-algebra"],
            free_form="note one",
            conflict_of_interest=["alice"],
        )
        self.pref2 = ReviewerPreference.objects.create(
            user=self.user,
            repository=self.repo2,
            maximum_capacity=4,
            auto_assign=False,
            notifications_enabled=False,
            preferred_labels=["t-analysis"],
            free_form="note two",
            conflict_of_interest=[],
        )
        LabelDef.objects.bulk_create(
            [
                LabelDef(repository=self.repo1, name="t-algebra", color="111111"),
                LabelDef(repository=self.repo1, name="t-number-theory", color="222222"),
                LabelDef(repository=self.repo1, name="CI", color="333333"),
                LabelDef(repository=self.repo1, name="maintainer-merge", color="444444"),
                LabelDef(repository=self.repo2, name="t-analysis", color="555555"),
                LabelDef(repository=self.repo2, name="IMO", color="666666"),
                LabelDef(repository=self.repo2, name="tech debt", color="777777"),
            ]
        )

    def _token(self) -> str:
        return issue_prefs_token(
            claims=PrefsLinkClaims(
                user_id=self.user.id,
                zulip_user_id=101,
                preference_ids=(self.pref1.id, self.pref2.id),
            )
        )

    def _post_data(self) -> tuple[dict[str, str | list[str]], dict[int, int]]:
        prefs = list(
            ReviewerPreference.objects.filter(id__in=[self.pref1.id, self.pref2.id]).order_by(
                "repository__owner",
                "repository__name",
                "id",
            )
        )
        data: dict[str, str | list[str]] = {
            "form-TOTAL_FORMS": str(len(prefs)),
            "form-INITIAL_FORMS": str(len(prefs)),
            "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": "1000",
        }
        index_by_pref_id: dict[int, int] = {}
        for idx, pref in enumerate(prefs):
            index_by_pref_id[pref.id] = idx
            data[f"form-{idx}-id"] = str(pref.id)
            data[f"form-{idx}-maximum_capacity"] = str(pref.maximum_capacity)
            data[f"form-{idx}-auto_assign"] = "on" if pref.auto_assign else ""
            data[f"form-{idx}-notifications_enabled"] = "on" if pref.notifications_enabled else ""
            settings = pref.notification_settings or {}
            data[f"form-{idx}-stale_nudge_days"] = str(settings.get("stale_nudge_days", DEFAULT_STALE_NUDGE_DAYS))
            data[f"form-{idx}-auto_unassign_days"] = str(settings.get("auto_unassign_days", DEFAULT_AUTO_UNASSIGN_DAYS))
            data[f"form-{idx}-away_until"] = ""
            data[f"form-{idx}-preferred_labels"] = list(pref.preferred_labels)
            data[f"form-{idx}-conflict_of_interest"] = "\n".join(pref.conflict_of_interest)
            data[f"form-{idx}-free_form"] = pref.free_form or ""
        return data, index_by_pref_id

    def test_get_with_valid_token_renders_real_form(self) -> None:
        token = self._token()
        response = self.client.get(reverse("zulip-prefs-form", kwargs={"token": token}))
        body = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Reviewer Preferences")
        self.assertContains(response, "Save Preferences")
        # Assert stable semantics (sections + fields), not exact help-text wording.
        self.assertContains(response, "Auto-Assignment")
        self.assertContains(response, "Notifications")
        self.assertContains(response, "Interests")
        self.assertIn('name="form-0-auto_assign"', body)
        self.assertIn('name="form-0-auto_unassign_days"', body)
        self.assertIn('name="form-0-away_until"', body)
        self.assertIn('name="form-0-maximum_capacity"', body)
        self.assertIn('name="form-0-notifications_enabled"', body)
        self.assertIn('name="form-0-stale_nudge_days"', body)
        self.assertIn('name="form-0-preferred_labels"', body)
        self.assertIn('name="form-0-free_form"', body)
        self.assertIn('name="form-0-conflict_of_interest"', body)
        self.assertContains(response, "t-number-theory")
        self.assertNotContains(response, "maintainer-merge")
        self.assertLess(body.index("Free form"), body.index("Conflict of interest"))
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_post_valid_updates_preferences_and_redirects(self) -> None:
        token = self._token()
        data, index_by_id = self._post_data()
        pref1_i = index_by_id[self.pref1.id]
        pref2_i = index_by_id[self.pref2.id]
        data[f"form-{pref1_i}-maximum_capacity"] = "7"
        data[f"form-{pref1_i}-notifications_enabled"] = "on"
        data[f"form-{pref1_i}-stale_nudge_days"] = "4"
        data[f"form-{pref1_i}-auto_unassign_days"] = "9"
        data[f"form-{pref1_i}-preferred_labels"] = ["t-algebra", "t-number-theory"]
        data[f"form-{pref2_i}-free_form"] = "updated note"
        data[f"form-{pref2_i}-auto_assign"] = "on"

        response = self.client.post(reverse("zulip-prefs-form", kwargs={"token": token}), data=data, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Preferences saved")
        self.pref1.refresh_from_db()
        self.pref2.refresh_from_db()
        self.assertEqual(self.pref1.maximum_capacity, 7)
        self.assertTrue(self.pref1.notifications_enabled)
        self.assertEqual(
            self.pref1.notification_settings,
            {"stale_nudge_days": 4, "auto_unassign_days": 9},
        )
        self.assertEqual(self.pref1.preferred_labels, ["t-algebra", "t-number-theory"])
        self.assertEqual(self.pref2.free_form, "updated note")
        self.assertTrue(self.pref2.auto_assign)

    def test_post_can_be_submitted_multiple_times_before_expiry(self) -> None:
        token = self._token()
        data, index_by_id = self._post_data()
        pref1_i = index_by_id[self.pref1.id]
        data[f"form-{pref1_i}-maximum_capacity"] = "6"
        response1 = self.client.post(reverse("zulip-prefs-form", kwargs={"token": token}), data=data)
        self.assertEqual(response1.status_code, 302)

        data2, index_by_id_2 = self._post_data()
        pref1_i_2 = index_by_id_2[self.pref1.id]
        data2[f"form-{pref1_i_2}-maximum_capacity"] = "3"
        response2 = self.client.post(reverse("zulip-prefs-form", kwargs={"token": token}), data=data2)
        self.assertEqual(response2.status_code, 302)

        self.pref1.refresh_from_db()
        self.assertEqual(self.pref1.maximum_capacity, 3)

    def test_post_invalid_shows_validation_errors(self) -> None:
        token = self._token()
        data, index_by_id = self._post_data()
        pref1_i = index_by_id[self.pref1.id]
        data[f"form-{pref1_i}-maximum_capacity"] = "0"

        response = self.client.post(reverse("zulip-prefs-form", kwargs={"token": token}), data=data)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ensure this value is greater than or equal to 1")
        self.pref1.refresh_from_db()
        self.assertEqual(self.pref1.maximum_capacity, 10)

    def test_post_invalid_notification_threshold_order_shows_validation_error(self) -> None:
        token = self._token()
        data, index_by_id = self._post_data()
        pref1_i = index_by_id[self.pref1.id]
        data[f"form-{pref1_i}-stale_nudge_days"] = "5"
        data[f"form-{pref1_i}-auto_unassign_days"] = "5"

        response = self.client.post(reverse("zulip-prefs-form", kwargs={"token": token}), data=data)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Auto-unassign days must be greater than stale nudge days.")
        self.pref1.refresh_from_db()
        self.assertEqual(self.pref1.notification_settings, {"stale_nudge_days": 2, "auto_unassign_days": 5})

    def test_post_blank_notification_thresholds_use_defaults(self) -> None:
        token = self._token()
        data, index_by_id = self._post_data()
        pref1_i = index_by_id[self.pref1.id]
        data[f"form-{pref1_i}-stale_nudge_days"] = ""
        data[f"form-{pref1_i}-auto_unassign_days"] = ""

        response = self.client.post(reverse("zulip-prefs-form", kwargs={"token": token}), data=data, follow=True)

        self.assertEqual(response.status_code, 200)
        self.pref1.refresh_from_db()
        self.assertEqual(
            self.pref1.notification_settings,
            {"stale_nudge_days": DEFAULT_STALE_NUDGE_DAYS, "auto_unassign_days": DEFAULT_AUTO_UNASSIGN_DAYS},
        )

    def test_post_rejects_auto_unassign_days_above_hard_max(self) -> None:
        token = self._token()
        data, index_by_id = self._post_data()
        pref1_i = index_by_id[self.pref1.id]
        data[f"form-{pref1_i}-stale_nudge_days"] = "14"
        data[f"form-{pref1_i}-auto_unassign_days"] = "22"

        response = self.client.post(reverse("zulip-prefs-form", kwargs={"token": token}), data=data)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ensure this value is less than or equal to 21.")
        self.pref1.refresh_from_db()
        self.assertEqual(self.pref1.notification_settings, {"stale_nudge_days": 2, "auto_unassign_days": 5})

    def test_post_rejects_unknown_preferred_label_choice(self) -> None:
        token = self._token()
        data, index_by_id = self._post_data()
        pref1_i = index_by_id[self.pref1.id]
        data[f"form-{pref1_i}-preferred_labels"] = ["not-a-real-label"]

        response = self.client.post(reverse("zulip-prefs-form", kwargs={"token": token}), data=data)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Select a valid choice")
        self.pref1.refresh_from_db()
        self.assertEqual(self.pref1.preferred_labels, ["t-algebra"])

    def test_get_shows_legacy_selected_labels_with_warning(self) -> None:
        self.pref1.preferred_labels = ["legacy-topic", "t-algebra"]
        self.pref1.save(update_fields=["preferred_labels"])
        token = self._token()

        response = self.client.get(reverse("zulip-prefs-form", kwargs={"token": token}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Legacy labels currently selected: legacy-topic.")
        self.assertContains(response, "legacy-topic (legacy: not in synced topic labels)")

    def test_invalid_token_returns_forbidden(self) -> None:
        response = self.client.get(reverse("zulip-prefs-form", kwargs={"token": "not-a-token"}))

        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "invalid", status_code=403)

    def test_expired_token_returns_forbidden(self) -> None:
        with patch("zulip_bot.services.prefs_links.time.time", return_value=1_700_000_000):
            token = issue_prefs_token(
                claims=PrefsLinkClaims(
                    user_id=self.user.id,
                    zulip_user_id=101,
                    preference_ids=(self.pref1.id, self.pref2.id),
                )
            )
        with patch("zulip_bot.services.prefs_links.time.time", return_value=1_700_000_000 + 1_900):
            response = self.client.get(reverse("zulip-prefs-form", kwargs={"token": token}))

        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "expired", status_code=403)

    def test_token_with_preferences_for_other_user_is_forbidden(self) -> None:
        other_user = User.objects.create(github_login="other", zulip_user_id=202)
        other_pref = ReviewerPreference.objects.create(
            user=other_user,
            repository=self.repo1,
        )
        token = issue_prefs_token(
            claims=PrefsLinkClaims(
                user_id=self.user.id,
                zulip_user_id=101,
                preference_ids=(self.pref1.id, other_pref.id),
            )
        )

        response = self.client.get(reverse("zulip-prefs-form", kwargs={"token": token}))
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "invalid", status_code=403)

    @override_settings(
        ZULIP_BASE_URL="https://leanprover.zulipchat.com",
        ZULIP_BOT_EMAIL="bot@example.com",
        ZULIP_BOT_API_KEY="bot-key",
    )
    def test_get_prefers_timezone_from_zulip_api(self) -> None:
        token = self._token()
        with patch("zulip_bot.views.ZulipClient.get_user_by_id", return_value={"user": {"timezone": "Europe/Berlin"}}):
            response = self.client.get(reverse("zulip-prefs-form", kwargs={"token": token}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Europe/Berlin")


class TestPrefsFormFieldCoverage(TestCase):
    def test_reviewer_preference_fields_are_accounted_for(self) -> None:
        missing, extra = reviewer_preference_unaccounted_fields()
        self.assertEqual(missing, set(), f"Unaccounted ReviewerPreference fields: {sorted(missing)}")
        self.assertEqual(extra, set(), f"Unknown configured fields: {sorted(extra)}")
