"""Maintainability guard for the reviewer-preferences form.

Lives in `core` because the form does (`core.forms.ReviewerPreferenceForm`) and because the
invariant is about the *model*: every `ReviewerPreference` field must be explicitly classified as
reviewer-editable or deliberately not. Adding a field without touching the form config fails here.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from core.forms import reviewer_preference_unaccounted_fields


class ReviewerPreferenceFormCoverageTests(SimpleTestCase):
    def test_reviewer_preference_fields_are_accounted_for(self) -> None:
        missing, extra = reviewer_preference_unaccounted_fields()
        self.assertEqual(missing, set(), f"Unaccounted ReviewerPreference fields: {sorted(missing)}")
        self.assertEqual(extra, set(), f"Unknown configured fields: {sorted(extra)}")
