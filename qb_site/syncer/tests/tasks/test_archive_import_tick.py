"""Tests for the archive_import_tick beat task (design doc 043 Commit 4)."""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from syncer.models import ArchiveImportItem, ArchiveImportItemStatus
from syncer.tasks.archive_import import archive_import_tick
from syncer.tests.factories import make_repo


class TestArchiveImportTick(TestCase):
    def setUp(self) -> None:
        self.repo = make_repo()

    def _enroll(self, *, pr_number: int, status: ArchiveImportItemStatus, last_attempted_at=None, attempts: int = 0):
        return ArchiveImportItem.objects.create(
            repository=self.repo,
            archive_name="queueboard-archive2",
            pr_number=pr_number,
            archive_path=f"data/{pr_number}/pr_info.json",
            status=status,
            last_attempted_at=last_attempted_at,
            attempts=attempts,
        )

    @override_settings(ARCHIVE_IMPORT_ENABLED=False)
    @mock.patch("syncer.tasks.archive_import.import_archive_pr_item.delay")
    def test_disabled_short_circuits_without_enqueueing(self, mock_delay) -> None:
        self._enroll(pr_number=1, status=ArchiveImportItemStatus.PENDING)
        result = archive_import_tick()
        self.assertEqual(result, {"status": "disabled", "enqueued": 0})
        mock_delay.assert_not_called()

    @override_settings(ARCHIVE_IMPORT_ENABLED=True, ARCHIVE_IMPORT_BATCH_SIZE=3)
    @mock.patch("syncer.tasks.archive_import.import_archive_pr_item.delay")
    def test_picks_up_to_batch_size_pending_rows(self, mock_delay) -> None:
        mock_delay.return_value.id = "task-id"
        for n in range(1, 6):
            self._enroll(pr_number=n, status=ArchiveImportItemStatus.PENDING)
        result = archive_import_tick()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["enqueued"], 3)
        self.assertEqual(mock_delay.call_count, 3)

    @override_settings(ARCHIVE_IMPORT_ENABLED=True, ARCHIVE_IMPORT_BATCH_SIZE=10)
    @mock.patch("syncer.tasks.archive_import.import_archive_pr_item.delay")
    def test_does_not_pick_in_progress_completed_or_failed_permanent(self, mock_delay) -> None:
        self._enroll(pr_number=1, status=ArchiveImportItemStatus.IN_PROGRESS)
        self._enroll(pr_number=2, status=ArchiveImportItemStatus.COMPLETED)
        self._enroll(pr_number=3, status=ArchiveImportItemStatus.FAILED_PERMANENT)
        self._enroll(pr_number=4, status=ArchiveImportItemStatus.SKIPPED)
        # The two below are pickable.
        pending = self._enroll(pr_number=5, status=ArchiveImportItemStatus.PENDING)
        transient = self._enroll(pr_number=6, status=ArchiveImportItemStatus.FAILED_TRANSIENT)

        archive_import_tick()
        picked = sorted(call.args[0] for call in mock_delay.call_args_list)
        self.assertEqual(picked, sorted([pending.pk, transient.pk]))

    @override_settings(ARCHIVE_IMPORT_ENABLED=True, ARCHIVE_IMPORT_BATCH_SIZE=2)
    @mock.patch("syncer.tasks.archive_import.import_archive_pr_item.delay")
    def test_orders_by_last_attempted_at_nulls_first(self, mock_delay) -> None:
        now = timezone.now()
        # NULL last_attempted_at (brand-new pending) goes ahead of older retries.
        recent_retry = self._enroll(
            pr_number=1,
            status=ArchiveImportItemStatus.FAILED_TRANSIENT,
            last_attempted_at=now - timedelta(minutes=1),
            attempts=1,
        )
        old_retry = self._enroll(
            pr_number=2,
            status=ArchiveImportItemStatus.FAILED_TRANSIENT,
            last_attempted_at=now - timedelta(hours=1),
            attempts=1,
        )
        brand_new = self._enroll(pr_number=3, status=ArchiveImportItemStatus.PENDING)

        archive_import_tick()
        picked = [call.args[0] for call in mock_delay.call_args_list]
        # Brand-new pending first (NULL last_attempted_at sorts first), then the
        # oldest retry. The recent retry is below the cap.
        self.assertEqual(picked, [brand_new.pk, old_retry.pk])
        self.assertNotIn(recent_retry.pk, picked)
