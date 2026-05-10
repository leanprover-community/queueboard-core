"""Convergence counters for the archive worklist (design doc 043 Commit 4)."""

from __future__ import annotations

from django.test import TestCase

from syncer.models import (
    ArchiveImportItem,
    ArchiveImportItemStatus,
    SyncerConvergenceSnapshot,
)
from syncer.tasks.collect_convergence import collect_syncer_convergence_task
from syncer.tests.factories import make_repo


class TestCollectSyncerConvergenceArchiveCounters(TestCase):
    def setUp(self) -> None:
        self.repo = make_repo(owner="leanprover-community", name="mathlib4")
        for n, status in [
            (1, ArchiveImportItemStatus.PENDING),
            (2, ArchiveImportItemStatus.PENDING),
            (3, ArchiveImportItemStatus.IN_PROGRESS),
            (4, ArchiveImportItemStatus.FAILED_TRANSIENT),
            (5, ArchiveImportItemStatus.COMPLETED),
            (6, ArchiveImportItemStatus.COMPLETED),
            (7, ArchiveImportItemStatus.COMPLETED),
            (8, ArchiveImportItemStatus.FAILED_PERMANENT),
            (9, ArchiveImportItemStatus.SKIPPED),
        ]:
            ArchiveImportItem.objects.create(
                repository=self.repo,
                archive_name="queueboard-archive2",
                pr_number=n,
                archive_path=f"data/{n}/pr_info.json",
                status=status,
            )

    def test_archive_counters_reflect_worklist_state(self) -> None:
        collect_syncer_convergence_task()
        snap = SyncerConvergenceSnapshot.objects.filter(repository=self.repo).latest("collected_at")
        # Pending rolls in pending + in_progress + failed_transient (work still
        # to do for the importer).
        self.assertEqual(snap.archive_pending, 4)
        self.assertEqual(snap.archive_completed, 3)
        self.assertEqual(snap.archive_failed_permanent, 1)
