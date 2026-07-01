"""Tests for the resync_archive_touched_prs command."""

from __future__ import annotations

import io
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import TestCase

from syncer.models import ArchiveImportItem, ArchiveImportItemStatus
from syncer.tests.factories import make_pr, make_repo


class TestResyncArchiveTouchedPRsCommand(TestCase):
    def setUp(self) -> None:
        self.repo = make_repo(owner="leanprover-community", name="mathlib4")
        self.pr = make_pr(self.repo, 42, state="closed")
        ArchiveImportItem.objects.create(
            repository=self.repo,
            archive_name="queueboard-archive2",
            pr_number=42,
            archive_path="data/42/pr_info.json",
            status=ArchiveImportItemStatus.COMPLETED,
        )

    def test_dry_run_reports_but_does_not_enqueue(self) -> None:
        out = io.StringIO()
        with mock.patch("syncer.management.commands.resync_archive_touched_prs.sync_pr_task") as task:
            call_command("resync_archive_touched_prs", stdout=out)
            task.delay.assert_not_called()
        output = out.getvalue()
        self.assertIn("1 archive-touched live PR(s)", output)
        self.assertIn("#42", output)
        self.assertIn("dry-run", output)

    def test_apply_enqueues_forced_sync(self) -> None:
        out = io.StringIO()
        with mock.patch("syncer.management.commands.resync_archive_touched_prs.sync_pr_task") as task:
            call_command("resync_archive_touched_prs", "--apply", stdout=out)
            task.delay.assert_called_once_with(self.repo.id, 42, force=True)
        self.assertIn("Enqueued 1", out.getvalue())

    def test_importer_created_pr_is_not_enqueued(self) -> None:
        # An importer-created PR (archive_imported_at set) is not a regression target.
        from django.utils import timezone

        made = make_pr(self.repo, 99, state="closed", archive_imported_at=timezone.now())
        ArchiveImportItem.objects.create(
            repository=self.repo,
            archive_name="queueboard-archive2",
            pr_number=99,
            archive_path="data/99/pr_info.json",
            status=ArchiveImportItemStatus.COMPLETED,
        )
        out = io.StringIO()
        with mock.patch("syncer.management.commands.resync_archive_touched_prs.sync_pr_task") as task:
            call_command("resync_archive_touched_prs", "--apply", stdout=out)
            enqueued_numbers = {call.args[1] for call in task.delay.call_args_list}
        self.assertNotIn(made.number, enqueued_numbers)
        self.assertIn(42, enqueued_numbers)

    def test_repo_filter_validates_format(self) -> None:
        with self.assertRaises(CommandError):
            call_command("resync_archive_touched_prs", "--repo", "no-slash")
