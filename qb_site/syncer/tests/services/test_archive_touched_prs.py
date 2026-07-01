"""Tests for archive_touched_live_prs_queryset (design doc 043 follow-up)."""

from __future__ import annotations

from django.test import TestCase
from django.utils import timezone

from syncer.models import ArchiveImportItem, ArchiveImportItemStatus
from syncer.services.archive_import import archive_touched_live_prs_queryset
from syncer.tests.factories import make_pr, make_repo


class TestArchiveTouchedLivePRsQueryset(TestCase):
    def setUp(self) -> None:
        self.repo = make_repo(owner="leanprover-community", name="mathlib4")

    def _item(self, number: int, status=ArchiveImportItemStatus.COMPLETED, archive="queueboard-archive2"):
        return ArchiveImportItem.objects.create(
            repository=self.repo,
            archive_name=archive,
            pr_number=number,
            archive_path=f"data/{number}/pr_info.json",
            status=status,
        )

    def test_matches_preexisting_pr_with_completed_item(self) -> None:
        pr = make_pr(self.repo, 10, state="closed")  # archive_imported_at is null
        self._item(10)
        self.assertEqual([p.id for p in archive_touched_live_prs_queryset(self.repo)], [pr.id])

    def test_excludes_pr_created_by_importer(self) -> None:
        # Importer-created rows carry archive_imported_at; their scalars came
        # from the archive by design, so they are not a regression.
        make_pr(self.repo, 11, state="closed", archive_imported_at=timezone.now())
        self._item(11)
        self.assertEqual(archive_touched_live_prs_queryset(self.repo).count(), 0)

    def test_excludes_pr_with_no_completed_item(self) -> None:
        make_pr(self.repo, 12, state="closed")
        self._item(12, status=ArchiveImportItemStatus.PENDING)
        self.assertEqual(archive_touched_live_prs_queryset(self.repo).count(), 0)

    def test_matches_when_any_archive_item_completed(self) -> None:
        # A PR present in both archives has two rows; one completed is enough.
        pr = make_pr(self.repo, 13, state="closed")
        self._item(13, status=ArchiveImportItemStatus.FAILED_PERMANENT, archive="queueboard-archive")
        self._item(13, status=ArchiveImportItemStatus.COMPLETED, archive="queueboard-archive2")
        self.assertEqual([p.id for p in archive_touched_live_prs_queryset(self.repo)], [pr.id])

    def test_repository_scope_is_respected(self) -> None:
        make_pr(self.repo, 14, state="closed")
        self._item(14)
        other = make_repo(owner="o2", name="r2")
        self.assertEqual(archive_touched_live_prs_queryset(other).count(), 0)
        self.assertEqual(archive_touched_live_prs_queryset(None).count(), 1)
