"""``excluded_labels`` in the reviewer-topics.json import/export (design doc 057)."""

from __future__ import annotations

import io
import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import Repository, ReviewerPreference, User
from core.services.reviewer_topics_importer import (
    ReviewerTopicsImportError,
    export_reviewer_topics,
    import_reviewer_topics,
)

REPO = "leanprover-community/mathlib4"


class ReviewerTopicsExcludedLabelsTests(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        self.user = User.objects.create(github_login="alice")
        self.pref = ReviewerPreference.objects.create(repository=self.repo, user=self.user, excluded_labels=["WIP"])

    def _import(self, entries: list[dict], **kwargs) -> None:
        import_reviewer_topics(repo=REPO, file_obj=io.StringIO(json.dumps(entries)), **kwargs)

    def test_import_replaces_and_dedupes_case_insensitively(self) -> None:
        self._import([{"github_handle": "alice", "excluded_labels": ["LLM-generated", "llm-generated", "easy"]}])
        self.pref.refresh_from_db()
        self.assertEqual(self.pref.excluded_labels, ["LLM-generated", "easy"])

    def test_import_cleans_malformed_values(self) -> None:
        # A bare string is one label, not its characters; `null` entries are dropped, not stored as "None".
        self._import([{"github_handle": "alice", "excluded_labels": "LLM-generated"}])
        self.pref.refresh_from_db()
        self.assertEqual(self.pref.excluded_labels, ["LLM-generated"])

        self._import([{"github_handle": "alice", "excluded_labels": [" easy ", None, 3]}])
        self.pref.refresh_from_db()
        self.assertEqual(self.pref.excluded_labels, ["easy"])

    def test_import_without_the_key_leaves_the_value_alone(self) -> None:
        # A file written before 057 must never silently clear a reviewer's exclusions.
        self._import([{"github_handle": "alice", "maximum_capacity": 4}])
        self.pref.refresh_from_db()
        self.assertEqual(self.pref.excluded_labels, ["WIP"])

    def test_import_refuses_a_label_both_preferred_and_excluded(self) -> None:
        # Nothing is written, not even the valid entry before it: the operator fixes the file.
        entries = [
            {"github_handle": "carol", "top_level": ["t-order"]},
            {"github_handle": "alice", "top_level": ["t-algebra"], "excluded_labels": ["T-Algebra"]},
        ]
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run):
                with self.assertRaisesMessage(ReviewerTopicsImportError, "alice: T-Algebra"):
                    self._import(entries, dry_run=dry_run)
                self.pref.refresh_from_db()
                self.assertEqual(self.pref.excluded_labels, ["WIP"])
                self.assertFalse(User.objects.filter(github_login="carol").exists())

    def test_import_refuses_an_overlap_with_the_stored_other_half(self) -> None:
        # The entry sets only preferred labels, but the stored exclusions already hold one of them.
        with self.assertRaisesMessage(ReviewerTopicsImportError, "alice: WIP"):
            self._import([{"github_handle": "alice", "top_level": ["wip", "t-algebra"]}])

    def test_export_round_trips(self) -> None:
        _owner, _name, entries = export_reviewer_topics(repo=REPO)
        self.assertEqual(entries[0]["excluded_labels"], ["WIP"])

        self.pref.excluded_labels = []
        self.pref.save(update_fields=["excluded_labels"])
        _owner, _name, entries = export_reviewer_topics(repo=REPO)
        self.assertNotIn("excluded_labels", entries[0])


class ReviewerTopicsExportPageTests(TestCase):
    def test_export_page_warns_that_the_file_is_private(self) -> None:
        # The format is the one published on a public branch; the page an operator uses must say
        # that this export is not safe to publish as is.
        admin_user = get_user_model().objects.create_superuser(username="admin", email="a@example.com", password="pw")
        self.client.force_login(admin_user)
        response = self.client.get(reverse("admin:core_reviewerpreference_export_topics"))
        self.assertContains(response, "This file contains private reviewer preferences")
        self.assertContains(response, "excluded_labels")
