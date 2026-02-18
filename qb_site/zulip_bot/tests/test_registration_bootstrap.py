from __future__ import annotations

from django.test import TestCase

from core.models import Repository, ReviewerPreference, User
from zulip_bot.services.registration_bootstrap import ensure_default_preferences_for_user


class TestRegistrationBootstrap(TestCase):
    def test_creates_default_preferences_for_active_repositories(self) -> None:
        user = User.objects.create(github_login="reviewer")
        active_1 = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        active_2 = Repository.objects.create(owner="leanprover", name="stdlib", default_branch="master")
        Repository.objects.create(owner="other", name="inactive", default_branch="main", is_active=False)

        result = ensure_default_preferences_for_user(user=user)

        self.assertEqual(result.active_repository_count, 2)
        self.assertEqual(result.created_count, 2)
        self.assertEqual(result.existing_count, 0)
        self.assertTrue(ReviewerPreference.objects.filter(user=user, repository=active_1).exists())
        self.assertTrue(ReviewerPreference.objects.filter(user=user, repository=active_2).exists())
        self.assertEqual(ReviewerPreference.objects.filter(user=user).count(), 2)

    def test_is_idempotent_when_preferences_already_exist(self) -> None:
        user = User.objects.create(github_login="reviewer")
        active = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        ReviewerPreference.objects.create(user=user, repository=active)

        result = ensure_default_preferences_for_user(user=user)

        self.assertEqual(result.active_repository_count, 1)
        self.assertEqual(result.created_count, 0)
        self.assertEqual(result.existing_count, 1)
        self.assertEqual(ReviewerPreference.objects.filter(user=user, repository=active).count(), 1)
