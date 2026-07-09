from __future__ import annotations

import importlib
import json
import io
from types import SimpleNamespace

from django.apps import apps as global_apps
from django.db import connection
from django.test import TestCase

from core.models import Repository, ReviewerPreference, User
from core.services.reviewer_topics_importer import import_reviewer_topics

REPO = "leanprover-community/mathlib4"


def _make_repo() -> Repository:
    return Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master", is_active=True)


class AssignmentAcceptanceDefaultTest(TestCase):
    """New reviewer accounts default to `confirm` (design doc 050)."""

    def test_field_default_is_confirm(self):
        field = ReviewerPreference._meta.get_field("assignment_acceptance")
        self.assertEqual(field.default, ReviewerPreference.ACCEPTANCE_CONFIRM)

    def test_new_preference_defaults_to_confirm(self):
        repo = _make_repo()
        user = User.objects.create(github_login="brand-new-reviewer", is_active=True)
        pref = ReviewerPreference.objects.create(repository=repo, user=user)
        self.assertEqual(pref.assignment_acceptance, ReviewerPreference.ACCEPTANCE_CONFIRM)


class AssignmentAcceptanceBackfillTest(TestCase):
    """The data migration grandfathers existing reviewers to `auto`."""

    def test_backfill_function_sets_all_rows_to_auto(self):
        repo = _make_repo()
        # Rows created here take the model default (`confirm`), standing in for pre-existing rows.
        for login in ("rev-a", "rev-b"):
            user = User.objects.create(github_login=login, is_active=True)
            ReviewerPreference.objects.create(repository=repo, user=user)
        self.assertTrue(all(p.assignment_acceptance == "confirm" for p in ReviewerPreference.objects.all()))

        migration_mod = importlib.import_module("core.migrations.0007_reviewerpreference_assignment_acceptance")
        migration_mod.backfill_existing_to_auto(global_apps, SimpleNamespace(connection=connection))

        self.assertTrue(all(p.assignment_acceptance == "auto" for p in ReviewerPreference.objects.all()))


class AssignmentAcceptanceImporterTest(TestCase):
    """The reviewer-topics importer must never overwrite an existing reviewer's acceptance mode."""

    def _import(self, entries: list[dict]) -> None:
        import_reviewer_topics(repo=REPO, file_obj=io.StringIO(json.dumps(entries)))

    def test_reimport_does_not_flip_existing_acceptance(self):
        repo = _make_repo()
        user = User.objects.create(github_login="grandfathered", is_active=True)
        pref = ReviewerPreference.objects.create(
            repository=repo,
            user=user,
            assignment_acceptance=ReviewerPreference.ACCEPTANCE_AUTO,
            maximum_capacity=5,
        )

        # Re-import touches an unrelated field (capacity) but must leave acceptance alone.
        self._import([{"github_handle": "grandfathered", "maximum_capacity": 9}])

        pref.refresh_from_db()
        self.assertEqual(pref.maximum_capacity, 9)
        self.assertEqual(pref.assignment_acceptance, ReviewerPreference.ACCEPTANCE_AUTO)

    def test_import_creates_new_reviewer_as_confirm(self):
        # A brand-new handle imported after the migration inherits the `confirm` default.
        self._import([{"github_handle": "freshly-imported", "top_level": ["t-algebra"]}])

        pref = ReviewerPreference.objects.get(user__github_login="freshly-imported")
        self.assertEqual(pref.assignment_acceptance, ReviewerPreference.ACCEPTANCE_CONFIRM)
