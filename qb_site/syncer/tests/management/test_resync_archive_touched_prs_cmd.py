"""Tests for the resync_archive_touched_prs command."""

from __future__ import annotations

import io
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import TestCase
from django.utils import timezone

from syncer.models import ArchiveImportItem, ArchiveImportItemStatus
from syncer.tests.factories import make_pr, make_repo


class TestResyncArchiveTouchedPRsCommand(TestCase):
    def setUp(self) -> None:
        self.repo = make_repo(owner="leanprover-community", name="mathlib4")
        self.touch = timezone.now() - timezone.timedelta(days=7)
        self.pr = make_pr(self.repo, 42, state="closed", last_synced_at=self.touch - timezone.timedelta(days=1))
        self._item(42)

    def _item(self, number: int, completed_at=None, archive="queueboard-archive2"):
        return ArchiveImportItem.objects.create(
            repository=self.repo,
            archive_name=archive,
            pr_number=number,
            archive_path=f"data/{number}/pr_info.json",
            status=ArchiveImportItemStatus.COMPLETED,
            completed_at=completed_at if completed_at is not None else self.touch,
        )

    def _enqueued_numbers(self, *args: str) -> list[int]:
        out = io.StringIO()
        with mock.patch("syncer.management.commands.resync_archive_touched_prs.sync_pr_task") as task:
            call_command("resync_archive_touched_prs", "--apply", *args, stdout=out)
            return [call.args[1] for call in task.delay.call_args_list]

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
        made = make_pr(self.repo, 99, state="closed", archive_imported_at=timezone.now())
        self._item(99)
        enqueued = self._enqueued_numbers()
        self.assertNotIn(made.number, enqueued)
        self.assertIn(42, enqueued)

    def test_healed_pr_is_skipped_by_default(self) -> None:
        healed = make_pr(self.repo, 50, state="closed", last_synced_at=self.touch + timezone.timedelta(days=1))
        self._item(50)
        out = io.StringIO()
        with mock.patch("syncer.management.commands.resync_archive_touched_prs.sync_pr_task") as task:
            call_command("resync_archive_touched_prs", "--apply", stdout=out)
            enqueued = [call.args[1] for call in task.delay.call_args_list]
        self.assertNotIn(healed.number, enqueued)
        self.assertIn(42, enqueued)
        output = out.getvalue()
        self.assertIn("2 archive-touched live PR(s)", output)
        self.assertIn("1 already healed", output)

    def test_include_healed_targets_full_touched_set(self) -> None:
        healed = make_pr(self.repo, 50, state="closed", last_synced_at=self.touch + timezone.timedelta(days=1))
        self._item(50)
        enqueued = self._enqueued_numbers("--include-healed")
        self.assertIn(healed.number, enqueued)
        self.assertIn(42, enqueued)

    def test_never_synced_pr_is_enqueued(self) -> None:
        # NULL last_synced_at must survive the healed exclusion (SQL NULL trap).
        never = make_pr(self.repo, 51, state="closed", last_synced_at=None)
        self._item(51)
        self.assertIn(never.number, self._enqueued_numbers())

    def test_ordering_open_first_then_stalest_sync(self) -> None:
        # setUp PR #42: closed, synced touch-1d.
        make_pr(self.repo, 60, state="open", last_synced_at=self.touch - timezone.timedelta(days=2))
        self._item(60)
        make_pr(self.repo, 61, state="open", last_synced_at=None)
        self._item(61)
        make_pr(self.repo, 62, state="closed", last_synced_at=None)
        self._item(62)
        # Open before closed; within each state NULL last_synced_at first, then stalest.
        self.assertEqual(self._enqueued_numbers(), [61, 60, 62, 42])

    def test_limit_takes_prefix_of_ordering(self) -> None:
        make_pr(self.repo, 60, state="open", last_synced_at=self.touch - timezone.timedelta(days=2))
        self._item(60)
        self.assertEqual(self._enqueued_numbers("--limit", "1"), [60])

    def test_repo_filter_validates_format(self) -> None:
        with self.assertRaises(CommandError):
            call_command("resync_archive_touched_prs", "--repo", "no-slash")
