from __future__ import annotations

from django.test import TestCase

from core.models import Repository, ReviewerPreference, User
from zulip_bot.services.assignment_command_parser import GitHubPullRequestRef
from zulip_bot.services.assignment_validation import validate_assignment_targets


class TestAssignmentValidation(TestCase):
    def test_valid_target_with_preference(self) -> None:
        repo = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        user = User.objects.create(zulip_user_id=101, github_login="reviewer")
        ReviewerPreference.objects.create(repository=repo, user=user)

        result = validate_assignment_targets(
            pr=GitHubPullRequestRef(owner="leanprover-community", repo="mathlib4", number=123),
            target_user_ids=(101,),
        )

        self.assertIsNotNone(result.repository)
        self.assertEqual(len(result.targets), 1)
        self.assertTrue(result.targets[0].ok)
        self.assertEqual(result.targets[0].github_login, "reviewer")

    def test_unknown_reviewer(self) -> None:
        Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")

        result = validate_assignment_targets(
            pr=GitHubPullRequestRef(owner="leanprover-community", repo="mathlib4", number=123),
            target_user_ids=(999,),
        )

        self.assertEqual(result.targets[0].code, "unknown_reviewer")
        self.assertFalse(result.targets[0].ok)

    def test_missing_github_login(self) -> None:
        Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        User.objects.create(zulip_user_id=101, github_login="")

        result = validate_assignment_targets(
            pr=GitHubPullRequestRef(owner="leanprover-community", repo="mathlib4", number=123),
            target_user_ids=(101,),
        )

        self.assertEqual(result.targets[0].code, "missing_github_login")

    def test_missing_reviewer_preference(self) -> None:
        Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        User.objects.create(zulip_user_id=101, github_login="reviewer")

        result = validate_assignment_targets(
            pr=GitHubPullRequestRef(owner="leanprover-community", repo="mathlib4", number=123),
            target_user_ids=(101,),
        )

        self.assertEqual(result.targets[0].code, "missing_preference")

    def test_repository_not_configured(self) -> None:
        User.objects.create(zulip_user_id=101, github_login="reviewer")

        result = validate_assignment_targets(
            pr=GitHubPullRequestRef(owner="leanprover-community", repo="mathlib4", number=123),
            target_user_ids=(101,),
        )

        self.assertIsNone(result.repository)
        self.assertEqual(result.targets[0].code, "repository_not_configured")
