from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from syncer.models import RepoDiscoveryState
from syncer.tests.factories import make_repo


class RepoDiscoveryStateModelTests(TestCase):
    def setUp(self) -> None:
        self.repo = make_repo(owner="leanprover-community", name="mathlib4")
        self.state = RepoDiscoveryState.objects.create(repository=self.repo)

    def test_mark_attempted_sets_last_attempted_at(self) -> None:
        self.assertIsNone(self.state.last_attempted_at)

        self.state.mark_attempted()
        self.state.refresh_from_db()

        self.assertIsNotNone(self.state.last_attempted_at)

    def test_set_continuation_sets_cutoff_cursor_and_started_once(self) -> None:
        cutoff = timezone.now() - timedelta(minutes=10)

        self.state.set_continuation(cutoff_at=cutoff, cursor="CUR-1")
        self.state.refresh_from_db()
        first_started_at = self.state.continuation_started_at

        self.assertEqual(self.state.continuation_cutoff_at, cutoff)
        self.assertEqual(self.state.continuation_cursor, "CUR-1")
        self.assertIsNotNone(first_started_at)
        self.assertIsNotNone(self.state.last_attempted_at)

        self.state.set_continuation(cutoff_at=cutoff, cursor="CUR-2")
        self.state.refresh_from_db()

        self.assertEqual(self.state.continuation_cursor, "CUR-2")
        self.assertEqual(self.state.continuation_started_at, first_started_at)

    def test_mark_success_advances_watermark_and_clears_continuation(self) -> None:
        cutoff = timezone.now() - timedelta(minutes=15)
        self.state.set_continuation(cutoff_at=cutoff, cursor="CUR-1")

        self.state.mark_success(cutoff_at=cutoff)
        self.state.refresh_from_db()

        self.assertEqual(self.state.last_successful_cutoff_at, cutoff)
        self.assertIsNotNone(self.state.last_successful_at)
        self.assertIsNotNone(self.state.last_attempted_at)
        self.assertIsNone(self.state.continuation_cutoff_at)
        self.assertIsNone(self.state.continuation_cursor)
        self.assertIsNone(self.state.continuation_started_at)
