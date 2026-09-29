"""``excluded_labels`` in the reviewer-topics.json import/export (design doc 057)."""

from __future__ import annotations

import io
import json

from django.test import TestCase

from core.models import Repository, ReviewerPreference, User
from core.services.reviewer_topics_importer import export_reviewer_topics, import_reviewer_topics

REPO = "leanprover-community/mathlib4"


class ReviewerTopicsExcludedLabelsTests(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        self.user = User.objects.create(github_login="alice")
        self.pref = ReviewerPreference.objects.create(repository=self.repo, user=self.user, excluded_labels=["WIP"])

    def _import(self, entries: list[dict]) -> None:
        import_reviewer_topics(repo=REPO, file_obj=io.StringIO(json.dumps(entries)))

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

    def test_export_round_trips(self) -> None:
        _owner, _name, entries = export_reviewer_topics(repo=REPO)
        self.assertEqual(entries[0]["excluded_labels"], ["WIP"])

        self.pref.excluded_labels = []
        self.pref.save(update_fields=["excluded_labels"])
        _owner, _name, entries = export_reviewer_topics(repo=REPO)
        self.assertNotIn("excluded_labels", entries[0])
