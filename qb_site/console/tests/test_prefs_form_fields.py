"""Field, validation and label-catalog behavior of the reviewer preferences form.

Ported from `zulip_bot/tests/test_prefs_form.py` when the expiring token page was retired (design
doc 022, phase 3). The form itself (`core.forms.ReviewerPreferenceForm`) and its assembly
(`core.services.reviewer_prefs`) are unchanged by that move, so these exercise the same behavior
through the surface that now owns it — `/console/preferences/`.
"""

from __future__ import annotations

from datetime import timedelta

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from analyzer.models import ReviewerAssignmentApplication

from console.session import SESSION_USER_KEY
from core.models import Repository, ReviewerPreference, User
from core.services.reviewer_notification_settings import DEFAULT_AUTO_UNASSIGN_DAYS, DEFAULT_STALE_NUDGE_DAYS
from syncer.models import LabelDef


class ConsolePrefsFormFieldTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = User.objects.create(github_login="reviewer", github_node_id="node-reviewer", zulip_user_id=101)
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
        self.url = reverse("console:prefs")
        session = self.client.session
        session[SESSION_USER_KEY] = int(self.user.id)
        session.save()

    def _post_data(self) -> tuple[dict[str, object], dict[int, int]]:
        prefs = list(ReviewerPreference.objects.filter(user=self.user).order_by("repository__owner", "repository__name", "id"))
        data: dict[str, object] = {
            "form-TOTAL_FORMS": str(len(prefs)),
            "form-INITIAL_FORMS": str(len(prefs)),
            "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": "1000",
        }
        index_by_pref_id: dict[int, int] = {}
        for idx, pref in enumerate(prefs):
            index_by_pref_id[pref.id] = idx
            policy = pref.notification_settings or {}
            data[f"form-{idx}-id"] = str(pref.id)
            data[f"form-{idx}-maximum_capacity"] = str(pref.maximum_capacity)
            data[f"form-{idx}-max_new_assignments_per_week"] = (
                "" if pref.max_new_assignments_per_week is None else str(pref.max_new_assignments_per_week)
            )
            data[f"form-{idx}-auto_assign"] = "on" if pref.auto_assign else ""
            data[f"form-{idx}-assignment_acceptance"] = pref.assignment_acceptance
            data[f"form-{idx}-notifications_enabled"] = "on" if pref.notifications_enabled else ""
            data[f"form-{idx}-stale_nudge_days"] = str(policy.get("stale_nudge_days", DEFAULT_STALE_NUDGE_DAYS))
            data[f"form-{idx}-auto_unassign_days"] = str(policy.get("auto_unassign_days", DEFAULT_AUTO_UNASSIGN_DAYS))
            data[f"form-{idx}-away_until"] = ""
            data[f"form-{idx}-preferred_labels"] = list(pref.preferred_labels or [])
            data[f"form-{idx}-conflict_of_interest"] = "\n".join(pref.conflict_of_interest or [])
            data[f"form-{idx}-free_form"] = pref.free_form or ""
        return data, index_by_pref_id

    # ---- rendering -----------------------------------------------------

    def test_get_renders_the_expected_sections_and_fields(self) -> None:
        response = self.client.get(self.url)
        body = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        # Stable semantics (sections + field names), not exact help-text wording.
        self.assertContains(response, "Auto-Assignment")
        self.assertContains(response, "Notifications")
        self.assertContains(response, "Interests")
        for field in (
            "auto_assign",
            "auto_unassign_days",
            "away_until",
            "maximum_capacity",
            "max_new_assignments_per_week",
            "notifications_enabled",
            "stale_nudge_days",
            "preferred_labels",
            "free_form",
            "conflict_of_interest",
        ):
            self.assertIn(f'name="form-0-{field}"', body)
        self.assertLess(body.index("Free form"), body.index("Conflict of interest"))

    @override_settings(ANALYZER_ASSIGNMENT_PROPOSALS_ENABLED=True)
    def test_get_shows_assignment_acceptance_when_proposals_enabled(self) -> None:
        response = self.client.get(self.url)
        self.assertIn('name="form-0-assignment_acceptance"', response.content.decode("utf-8"))

    @override_settings(ANALYZER_ASSIGNMENT_PROPOSALS_ENABLED=False)
    def test_get_hides_assignment_acceptance_when_proposals_disabled(self) -> None:
        response = self.client.get(self.url)
        self.assertNotIn("assignment_acceptance", response.content.decode("utf-8"))

    def test_get_shows_legacy_selected_labels_with_warning(self) -> None:
        self.pref1.preferred_labels = ["legacy-topic", "t-algebra"]
        self.pref1.save(update_fields=["preferred_labels"])

        response = self.client.get(self.url)

        self.assertContains(response, "Legacy labels currently selected: legacy-topic.")
        self.assertContains(response, "legacy-topic (legacy: not in synced topic labels)")

    def test_custom_repo_pattern_changes_offered_labels(self) -> None:
        # By default "maintainer-merge" is not a topic label; a per-repo pattern can opt it in.
        self.repo1.assignment_topic_label_pattern = r"t-.*|maintainer-merge"
        self.repo1.save(update_fields=["assignment_topic_label_pattern"])

        body = self.client.get(self.url).content.decode("utf-8")
        self.assertIn('value="maintainer-merge"', body)
        self.assertNotIn('value="CI"', body)  # no longer matches this repo's pattern

        data, index_by_id = self._post_data()
        data[f"form-{index_by_id[self.pref1.id]}-preferred_labels"] = ["t-algebra", "maintainer-merge"]
        self.assertEqual(self.client.post(self.url, data=data).status_code, 302)
        self.pref1.refresh_from_db()
        self.assertEqual(self.pref1.preferred_labels, ["t-algebra", "maintainer-merge"])

    # ---- acceptance mode -----------------------------------------------

    @override_settings(ANALYZER_ASSIGNMENT_PROPOSALS_ENABLED=True)
    def test_post_updates_assignment_acceptance(self) -> None:
        self.assertEqual(self.pref1.assignment_acceptance, ReviewerPreference.ACCEPTANCE_CONFIRM)
        data, index_by_id = self._post_data()
        data[f"form-{index_by_id[self.pref1.id]}-assignment_acceptance"] = ReviewerPreference.ACCEPTANCE_AUTO
        data[f"form-{index_by_id[self.pref2.id]}-assignment_acceptance"] = ReviewerPreference.ACCEPTANCE_CONFIRM

        self.assertEqual(self.client.post(self.url, data=data).status_code, 302)

        self.pref1.refresh_from_db()
        self.pref2.refresh_from_db()
        self.assertEqual(self.pref1.assignment_acceptance, ReviewerPreference.ACCEPTANCE_AUTO)
        self.assertEqual(self.pref2.assignment_acceptance, ReviewerPreference.ACCEPTANCE_CONFIRM)

    @override_settings(ANALYZER_ASSIGNMENT_PROPOSALS_ENABLED=True)
    def test_post_rejects_unknown_assignment_acceptance(self) -> None:
        data, index_by_id = self._post_data()
        data[f"form-{index_by_id[self.pref1.id]}-assignment_acceptance"] = "sometimes"

        response = self.client.post(self.url, data=data)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Select a valid choice")
        self.pref1.refresh_from_db()
        self.assertEqual(self.pref1.assignment_acceptance, ReviewerPreference.ACCEPTANCE_CONFIRM)

    @override_settings(ANALYZER_ASSIGNMENT_PROPOSALS_ENABLED=False)
    def test_post_ignores_assignment_acceptance_when_proposals_disabled(self) -> None:
        # With the feature off the field is not part of the form, so a crafted POST value must not
        # change the stored mode.
        data, index_by_id = self._post_data()
        data[f"form-{index_by_id[self.pref1.id]}-assignment_acceptance"] = ReviewerPreference.ACCEPTANCE_AUTO

        self.assertEqual(self.client.post(self.url, data=data).status_code, 302)

        self.pref1.refresh_from_db()
        self.assertEqual(self.pref1.assignment_acceptance, ReviewerPreference.ACCEPTANCE_CONFIRM)

    # ---- validation ----------------------------------------------------

    def test_post_invalid_capacity_shows_validation_error(self) -> None:
        data, index_by_id = self._post_data()
        data[f"form-{index_by_id[self.pref1.id]}-maximum_capacity"] = "0"

        response = self.client.post(self.url, data=data)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ensure this value is greater than or equal to 1")
        self.pref1.refresh_from_db()
        self.assertEqual(self.pref1.maximum_capacity, 10)

    # ---- rolling-window rate limit (design doc 054) ---------------------

    def test_post_sets_and_clears_the_rate_limit(self) -> None:
        data, index_by_id = self._post_data()
        i = index_by_id[self.pref1.id]

        data[f"form-{i}-max_new_assignments_per_week"] = "5"
        self.assertEqual(self.client.post(self.url, data=data).status_code, 302)
        self.pref1.refresh_from_db()
        self.assertEqual(self.pref1.max_new_assignments_per_week, 5)

        # Blank is the opt-out, and it is also the default: clearing the field restores unlimited
        # intake rather than leaving the last number in force.
        data[f"form-{i}-max_new_assignments_per_week"] = ""
        self.assertEqual(self.client.post(self.url, data=data).status_code, 302)
        self.pref1.refresh_from_db()
        self.assertIsNone(self.pref1.max_new_assignments_per_week)

    def test_post_rejects_a_zero_rate_limit(self) -> None:
        # "Never assign me anything" is `auto_assign` off, which says so on every surface; a rate of
        # zero would be the same thing spelled in a way only the engine gate could explain.
        data, index_by_id = self._post_data()
        data[f"form-{index_by_id[self.pref1.id]}-max_new_assignments_per_week"] = "0"

        response = self.client.post(self.url, data=data)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ensure this value is greater than or equal to 1")
        self.pref1.refresh_from_db()
        self.assertIsNone(self.pref1.max_new_assignments_per_week)

    def test_rate_limit_help_text_names_the_rolling_window_and_recent_intake(self) -> None:
        """A reviewer cannot pick a number they cannot see (design doc 054).

        Also pins the rolling-window wording over "this week": the calendar reading produces a
        "why am I blocked, it's Monday" bug report.
        """
        for pr_number in (1, 2, 3):
            ReviewerAssignmentApplication.objects.create(
                run_date=timezone.now().date(),
                repository=self.repo1,
                pr_number=pr_number,
                reviewer_login="REVIEWER",  # stored casing differs from the lookup on purpose
                status=ReviewerAssignmentApplication.STATUS_APPLIED,
                applied_at=timezone.now() - timedelta(days=1),
            )

        response = self.client.get(self.url)

        self.assertContains(response, "rolling 7 days, not a calendar week")
        self.assertContains(response, "assigned 3 new PRs in the last 7 days")
        # The label carries the period, so the help text must not restate the cap.
        self.assertNotContains(response, "Cap on how many new PRs")

    def test_both_capacity_gates_explain_which_kind_of_limit_they_are(self) -> None:
        """`maximum_capacity` and the rate limit sit side by side; a bare number beside an
        annotated one reads as an oversight, and nothing would distinguish stock from flow."""
        response = self.client.get(self.url)

        self.assertContains(response, "How many assigned PRs you can hold at once")
        self.assertContains(response, "Max new assignments per 7 days")

    def test_post_invalid_notification_threshold_order_shows_validation_error(self) -> None:
        data, index_by_id = self._post_data()
        i = index_by_id[self.pref1.id]
        data[f"form-{i}-stale_nudge_days"] = "5"
        data[f"form-{i}-auto_unassign_days"] = "5"

        response = self.client.post(self.url, data=data)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Auto-unassign days must be greater than stale nudge days.")
        self.pref1.refresh_from_db()
        self.assertEqual(self.pref1.notification_settings, {"stale_nudge_days": 2, "auto_unassign_days": 5})

    def test_post_blank_notification_thresholds_use_defaults(self) -> None:
        data, index_by_id = self._post_data()
        i = index_by_id[self.pref1.id]
        data[f"form-{i}-stale_nudge_days"] = ""
        data[f"form-{i}-auto_unassign_days"] = ""

        self.assertEqual(self.client.post(self.url, data=data).status_code, 302)

        self.pref1.refresh_from_db()
        self.assertEqual(
            self.pref1.notification_settings,
            {"stale_nudge_days": DEFAULT_STALE_NUDGE_DAYS, "auto_unassign_days": DEFAULT_AUTO_UNASSIGN_DAYS},
        )

    def test_post_rejects_auto_unassign_days_above_hard_max(self) -> None:
        data, index_by_id = self._post_data()
        i = index_by_id[self.pref1.id]
        data[f"form-{i}-stale_nudge_days"] = "14"
        data[f"form-{i}-auto_unassign_days"] = "22"

        response = self.client.post(self.url, data=data)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ensure this value is less than or equal to 21.")
        self.pref1.refresh_from_db()
        self.assertEqual(self.pref1.notification_settings, {"stale_nudge_days": 2, "auto_unassign_days": 5})

    def test_post_rejects_unknown_preferred_label_choice(self) -> None:
        data, index_by_id = self._post_data()
        data[f"form-{index_by_id[self.pref1.id]}-preferred_labels"] = ["not-a-real-label"]

        response = self.client.post(self.url, data=data)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Select a valid choice")
        self.pref1.refresh_from_db()
        self.assertEqual(self.pref1.preferred_labels, ["t-algebra"])

    def test_post_can_be_submitted_repeatedly(self) -> None:
        data, index_by_id = self._post_data()
        i = index_by_id[self.pref1.id]

        data[f"form-{i}-maximum_capacity"] = "6"
        self.assertEqual(self.client.post(self.url, data=data).status_code, 302)
        self.pref1.refresh_from_db()
        self.assertEqual(self.pref1.maximum_capacity, 6)

        data[f"form-{i}-maximum_capacity"] = "8"
        self.assertEqual(self.client.post(self.url, data=data).status_code, 302)
        self.pref1.refresh_from_db()
        self.assertEqual(self.pref1.maximum_capacity, 8)
