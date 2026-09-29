"""``ReviewerPreference.excluded_labels`` at the model layer (design doc 057)."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse

from core.models import Repository, ReviewerPreference, User
from core.models.reviewer_preference import clean_label_names, overlapping_labels


class CleanLabelNamesTests(SimpleTestCase):
    def test_keeps_stripped_non_empty_strings_in_order(self) -> None:
        self.assertEqual(clean_label_names([" WIP ", "CI", "", "  "]), ["WIP", "CI"])

    def test_drops_non_string_entries(self) -> None:
        # The column is free JSON: an admin edit can leave `null` or a number in the list.
        self.assertEqual(clean_label_names(["WIP", None, 3, ["CI"]]), ["WIP"])

    def test_bare_string_is_one_label_not_its_characters(self) -> None:
        self.assertEqual(clean_label_names("WIP"), ["WIP"])

    def test_other_values_are_empty(self) -> None:
        for value in (None, {}, {"WIP": True}, 3):
            with self.subTest(value=value):
                self.assertEqual(clean_label_names(value), [])


class PreferredExcludedOverlapTests(SimpleTestCase):
    def test_overlap_ignores_case_and_whitespace(self) -> None:
        self.assertEqual(overlapping_labels(["t-algebra", " WIP"], ["T-Algebra", "wip ", "CI"]), ["T-Algebra", "wip"])

    def test_model_clean_refuses_an_overlap_on_the_excluded_field(self) -> None:
        pref = ReviewerPreference(preferred_labels=["t-algebra"], excluded_labels=["T-Algebra"])
        with self.assertRaises(ValidationError) as ctx:
            pref.clean()
        self.assertEqual(ctx.exception.message_dict, {"excluded_labels": ["Also selected as a preferred label: T-Algebra."]})

    def test_model_clean_accepts_disjoint_lists(self) -> None:
        ReviewerPreference(preferred_labels=["t-algebra"], excluded_labels=["WIP"]).clean()


class ReviewerPreferenceAdminOverlapTests(TestCase):
    """Django admin edits go through the same model rule as the console form."""

    def setUp(self) -> None:
        admin_user = get_user_model().objects.create_superuser(username="admin", email="a@example.com", password="pw")
        self.client = Client()
        self.client.force_login(admin_user)
        repo = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        self.pref = ReviewerPreference.objects.create(
            repository=repo, user=User.objects.create(github_login="alice"), preferred_labels=["t-algebra"]
        )

    def _post(self, excluded_json: str):
        pref = self.pref
        data = {
            "repository": str(pref.repository_id),
            "user": str(pref.user_id),
            "maximum_capacity": str(pref.maximum_capacity),
            "max_new_assignments_per_week": "",
            "auto_assign": "on",
            "assignment_acceptance": pref.assignment_acceptance,
            "away_until_0": "",
            "away_until_1": "",
            "preferred_labels": '["t-algebra"]',
            "free_form": "",
            "conflict_of_interest": "[]",
            "excluded_labels": excluded_json,
            "notification_settings": "{}",
        }
        return self.client.post(reverse("admin:core_reviewerpreference_change", args=[pref.pk]), data)

    def test_admin_refuses_an_overlap(self) -> None:
        response = self._post('["T-Algebra"]')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Also selected as a preferred label: T-Algebra.")
        self.pref.refresh_from_db()
        self.assertEqual(self.pref.excluded_labels, [])

    def test_admin_saves_disjoint_lists(self) -> None:
        self.assertEqual(self._post('["WIP"]').status_code, 302)
        self.pref.refresh_from_db()
        self.assertEqual(self.pref.excluded_labels, ["WIP"])
