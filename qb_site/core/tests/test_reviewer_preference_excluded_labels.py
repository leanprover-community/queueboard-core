"""``ReviewerPreference.excluded_labels`` at the model layer (design doc 057)."""

from __future__ import annotations

from django.test import SimpleTestCase

from core.models.reviewer_preference import clean_label_names


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
