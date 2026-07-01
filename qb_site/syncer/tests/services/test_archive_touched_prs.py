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

    def _item(self, number: int, status=ArchiveImportItemStatus.COMPLETED, archive="queueboard-archive2", completed_at=None):
        return ArchiveImportItem.objects.create(
            repository=self.repo,
            archive_name=archive,
            pr_number=number,
            archive_path=f"data/{number}/pr_info.json",
            status=status,
            completed_at=completed_at,
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


class TestExcludeHealed(TestCase):
    """exclude_healed drops PRs a live sync already re-fetched after the last archive touch."""

    def setUp(self) -> None:
        self.repo = make_repo(owner="leanprover-community", name="mathlib4")
        self.touch = timezone.now() - timezone.timedelta(days=7)

    def _item(self, number: int, completed_at, archive="queueboard-archive2"):
        return ArchiveImportItem.objects.create(
            repository=self.repo,
            archive_name=archive,
            pr_number=number,
            archive_path=f"data/{number}/pr_info.json",
            status=ArchiveImportItemStatus.COMPLETED,
            completed_at=completed_at,
        )

    def _healed_count(self) -> int:
        return archive_touched_live_prs_queryset(self.repo, exclude_healed=True).count()

    def test_synced_after_touch_is_excluded(self) -> None:
        make_pr(self.repo, 20, state="closed", last_synced_at=self.touch + timezone.timedelta(days=1))
        self._item(20, self.touch)
        self.assertEqual(self._healed_count(), 0)
        # …but still present without the flag (superset semantics unchanged).
        self.assertEqual(archive_touched_live_prs_queryset(self.repo).count(), 1)

    def test_synced_before_touch_is_kept(self) -> None:
        pr = make_pr(self.repo, 21, state="closed", last_synced_at=self.touch - timezone.timedelta(days=1))
        self._item(21, self.touch)
        self.assertEqual([p.id for p in archive_touched_live_prs_queryset(self.repo, exclude_healed=True)], [pr.id])

    def test_never_synced_is_kept(self) -> None:
        # NULL last_synced_at must not be excluded (SQL NULL-comparison trap).
        pr = make_pr(self.repo, 22, state="closed", last_synced_at=None)
        self._item(22, self.touch)
        self.assertEqual([p.id for p in archive_touched_live_prs_queryset(self.repo, exclude_healed=True)], [pr.id])

    def test_null_completed_at_is_kept(self) -> None:
        # A completed item without completed_at cannot prove healing; keep the PR.
        pr = make_pr(self.repo, 23, state="closed", last_synced_at=timezone.now())
        self._item(23, None)
        self.assertEqual([p.id for p in archive_touched_live_prs_queryset(self.repo, exclude_healed=True)], [pr.id])

    def test_latest_touch_across_archives_wins(self) -> None:
        # Synced between the two touches → the later touch could still have
        # clobbered it, so it is NOT healed.
        later_touch = self.touch + timezone.timedelta(days=2)
        pr = make_pr(self.repo, 24, state="closed", last_synced_at=self.touch + timezone.timedelta(days=1))
        self._item(24, self.touch, archive="queueboard-archive")
        self._item(24, later_touch, archive="queueboard-archive2")
        self.assertEqual([p.id for p in archive_touched_live_prs_queryset(self.repo, exclude_healed=True)], [pr.id])

    def test_latest_touch_ignores_null_completed_at_rows(self) -> None:
        # A NULL completed_at on one archive's item must not mask a real,
        # older touch timestamp on the other (DESC would sort NULL first).
        make_pr(self.repo, 25, state="closed", last_synced_at=self.touch + timezone.timedelta(days=1))
        self._item(25, None, archive="queueboard-archive")
        self._item(25, self.touch, archive="queueboard-archive2")
        self.assertEqual(self._healed_count(), 0)
