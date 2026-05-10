from __future__ import annotations

from django.db import IntegrityError
from django.test import TestCase

from syncer.models import ArchiveImportItem, ArchiveImportItemStatus
from syncer.tests.factories import make_repo


class TestArchiveImportItemModel(TestCase):
    def setUp(self) -> None:
        self.repo = make_repo(owner="leanprover-community", name="mathlib4")

    def test_default_status_is_pending(self) -> None:
        item = ArchiveImportItem.objects.create(
            repository=self.repo,
            archive_name="queueboard-archive2",
            pr_number=42,
            archive_path="data/42/pr_info.json",
        )
        self.assertEqual(item.status, ArchiveImportItemStatus.PENDING)
        self.assertEqual(item.attempts, 0)
        self.assertEqual(item.last_error, "")
        self.assertIsNone(item.last_attempted_at)
        self.assertIsNone(item.completed_at)
        self.assertIsNone(item.archive_blob_sha)
        self.assertIsNone(item.archive_timestamp)

    def test_unique_constraint_archive_name_pr_number(self) -> None:
        ArchiveImportItem.objects.create(
            repository=self.repo,
            archive_name="queueboard-archive2",
            pr_number=42,
            archive_path="data/42/pr_info.json",
        )
        with self.assertRaises(IntegrityError):
            ArchiveImportItem.objects.create(
                repository=self.repo,
                archive_name="queueboard-archive2",
                pr_number=42,
                archive_path="data/42/pr_info.json",
            )

    def test_same_pr_number_in_different_archive_is_allowed(self) -> None:
        ArchiveImportItem.objects.create(
            repository=self.repo,
            archive_name="queueboard-archive2",
            pr_number=42,
            archive_path="data/42/pr_info.json",
        )
        # Per design doc 043 open question we keep one row per (archive, pr) so
        # the same PR number from a different archive must coexist.
        ArchiveImportItem.objects.create(
            repository=self.repo,
            archive_name="queueboard-archive",
            pr_number=42,
            archive_path="data/42/pr_info.json",
        )
        self.assertEqual(ArchiveImportItem.objects.filter(pr_number=42).count(), 2)
