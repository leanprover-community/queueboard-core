from __future__ import annotations

from django.test import TestCase
from rest_framework.test import APIClient

from core.models import Repository, ReviewerPreference, User
from syncer.models.label_def import LabelDef


class ReviewerInterestsViewTests(TestCase):
    def setUp(self) -> None:
        self.client = APIClient()
        self.repo = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        for label_name in ("t-analysis", "t-algebra", "tech debt"):
            LabelDef.objects.create(repository=self.repo, name=label_name, color="ededed")
        self.alice = User.objects.create(github_login="alice")
        self.bob = User.objects.create(github_login="bob")

    def test_filters_unknown_labels_and_canonicalizes_casing(self):
        ReviewerPreference.objects.create(
            repository=self.repo,
            user=self.alice,
            preferred_labels=["T-Analysis", "tech-debt", "t-algebra", "T-ANALYSIS"],
            free_form="Happy to help",
        )
        ReviewerPreference.objects.create(
            repository=self.repo,
            user=self.bob,
            preferred_labels=["t-Algebra"],
            free_form=None,
        )

        resp = self.client.get("/api/v1/reviewer-interests", {"repo": "leanprover-community/mathlib4"})

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["meta"], {"repo": "leanprover-community/mathlib4"})
        self.assertEqual(
            body["reviewers"],
            [
                {
                    "github_login": "alice",
                    "preferred_labels": ["t-analysis", "t-algebra"],
                    "free_form": "Happy to help",
                },
                {
                    "github_login": "bob",
                    "preferred_labels": ["t-algebra"],
                    "free_form": None,
                },
            ],
        )

    def test_returns_empty_label_list_when_no_catalog_entries(self):
        empty_repo = Repository.objects.create(owner="leanprover-community", name="empty", default_branch="master")
        ReviewerPreference.objects.create(
            repository=empty_repo,
            user=self.alice,
            preferred_labels=["whatever"],
            free_form=None,
        )

        resp = self.client.get("/api/v1/reviewer-interests", {"repo": "leanprover-community/empty"})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["reviewers"], [{"github_login": "alice", "preferred_labels": [], "free_form": None}])

    def test_never_exposes_private_preferences(self):
        # This endpoint is public (it feeds the community team page). Conflicts of interest and
        # excluded labels (design doc 057) are private and must never appear in it, not even
        # indirectly as label text.
        ReviewerPreference.objects.create(
            repository=self.repo,
            user=self.alice,
            preferred_labels=["t-analysis"],
            free_form=None,
            conflict_of_interest=["mallory"],
            excluded_labels=["tech debt"],
        )

        resp = self.client.get("/api/v1/reviewer-interests", {"repo": "leanprover-community/mathlib4"})

        self.assertEqual(resp.status_code, 200)
        [reviewer] = resp.json()["reviewers"]
        self.assertEqual(set(reviewer), {"github_login", "preferred_labels", "free_form"})
        self.assertNotIn("mallory", resp.content.decode())
        self.assertNotIn("tech debt", resp.content.decode())
