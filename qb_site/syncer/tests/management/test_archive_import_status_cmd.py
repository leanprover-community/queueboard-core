"""Tests for the archive_import_status command (design doc 043 Commit 4)."""

from __future__ import annotations

import io

from django.core.management import CommandError, call_command
from django.test import TestCase
from django.utils import timezone

from syncer.models import ArchiveImportItem, ArchiveImportItemStatus
from syncer.tests.factories import make_repo


class TestArchiveImportStatusCommand(TestCase):
    def setUp(self) -> None:
        self.repo = make_repo(owner="leanprover-community", name="mathlib4")

    def _enroll(self, *, archive: str, pr_number: int, status: ArchiveImportItemStatus, last_error: str = ""):
        return ArchiveImportItem.objects.create(
            repository=self.repo,
            archive_name=archive,
            pr_number=pr_number,
            archive_path=f"data/{pr_number}/pr_info.json",
            status=status,
            last_error=last_error,
            last_attempted_at=timezone.now(),
        )

    def test_empty_worklist_prints_nothing_to_report(self) -> None:
        out = io.StringIO()
        call_command("archive_import_status", stdout=out)
        self.assertIn("No archive worklist rows.", out.getvalue())

    def test_renders_per_archive_status_breakdown(self) -> None:
        self._enroll(archive="queueboard-archive2", pr_number=1, status=ArchiveImportItemStatus.PENDING)
        self._enroll(archive="queueboard-archive2", pr_number=2, status=ArchiveImportItemStatus.COMPLETED)
        self._enroll(archive="queueboard-archive2", pr_number=3, status=ArchiveImportItemStatus.COMPLETED)
        self._enroll(
            archive="queueboard-archive2",
            pr_number=4,
            status=ArchiveImportItemStatus.FAILED_PERMANENT,
            last_error="http_404: …",
        )
        out = io.StringIO()
        call_command("archive_import_status", stdout=out)
        text = out.getvalue()
        self.assertIn("archive: queueboard-archive2", text)
        self.assertIn("pending                   1", text)
        self.assertIn("completed                 2", text)
        self.assertIn("failed_permanent          1", text)
        # Recent error sample for the failed row.
        self.assertIn("PR #4", text)
        self.assertIn("http_404", text)

    def test_repo_filter_unknown_raises(self) -> None:
        with self.assertRaises(CommandError):
            call_command("archive_import_status", "--repo", "leanprover-community/does-not-exist")

    def test_repo_filter_must_be_owner_slash_name(self) -> None:
        with self.assertRaises(CommandError):
            call_command("archive_import_status", "--repo", "no-slash")

    def test_oldest_pending_is_reported(self) -> None:
        self._enroll(archive="queueboard-archive2", pr_number=42, status=ArchiveImportItemStatus.PENDING)
        self._enroll(archive="queueboard-archive2", pr_number=7, status=ArchiveImportItemStatus.PENDING)
        out = io.StringIO()
        call_command("archive_import_status", stdout=out)
        text = out.getvalue()
        # Lowest PR number is reported as oldest pending (ordered by pr_number).
        self.assertIn("oldest pending:    PR #7", text)
