from __future__ import annotations

import io
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import TestCase

from syncer.models import ArchiveImportItem, ArchiveImportItemStatus
from syncer.services.archive_bootstrap import ArchivePREntry
from syncer.tests.factories import make_repo


def _entries(*pr_numbers: int) -> list[ArchivePREntry]:
    return [ArchivePREntry(pr_number=n, archive_path=f"data/{n}/pr_info.json", blob_sha=f"sha-{n}") for n in pr_numbers]


class TestBootstrapArchiveWorklistCommand(TestCase):
    def setUp(self) -> None:
        self.repo = make_repo(owner="leanprover-community", name="mathlib4")

    def test_unknown_repo_raises(self) -> None:
        with self.assertRaises(CommandError):
            call_command(
                "bootstrap_archive_worklist",
                "--archive",
                "queueboard-archive2",
                "--repo",
                "leanprover-community/does-not-exist",
            )

    def test_repo_argument_must_be_owner_slash_name(self) -> None:
        with self.assertRaises(CommandError):
            call_command(
                "bootstrap_archive_worklist",
                "--archive",
                "queueboard-archive2",
                "--repo",
                "no-slash",
            )

    @mock.patch("syncer.management.commands.bootstrap_archive_worklist.enumerate_archive_pr_entries")
    def test_inserts_pending_rows_for_each_pr_dir(self, mock_enum) -> None:
        mock_enum.return_value = _entries(1, 5, 100)
        out = io.StringIO()
        call_command(
            "bootstrap_archive_worklist",
            "--archive",
            "queueboard-archive2",
            "--repo",
            "leanprover-community/mathlib4",
            stdout=out,
        )
        rows = list(
            ArchiveImportItem.objects.filter(repository=self.repo, archive_name="queueboard-archive2").order_by("pr_number")
        )
        self.assertEqual([r.pr_number for r in rows], [1, 5, 100])
        self.assertEqual([r.archive_path for r in rows], ["data/1/pr_info.json", "data/5/pr_info.json", "data/100/pr_info.json"])
        self.assertEqual([r.archive_blob_sha for r in rows], ["sha-1", "sha-5", "sha-100"])
        for row in rows:
            self.assertEqual(row.status, ArchiveImportItemStatus.PENDING)
            self.assertEqual(row.attempts, 0)
            self.assertIsNone(row.last_attempted_at)
            self.assertIsNone(row.completed_at)
        self.assertIn("Enrollment complete", out.getvalue())

    @mock.patch("syncer.management.commands.bootstrap_archive_worklist.enumerate_archive_pr_entries")
    def test_rerun_is_idempotent(self, mock_enum) -> None:
        mock_enum.return_value = _entries(1, 2, 3)
        call_command(
            "bootstrap_archive_worklist",
            "--archive",
            "queueboard-archive2",
            "--repo",
            "leanprover-community/mathlib4",
            stdout=io.StringIO(),
        )
        # Mutate one row to confirm re-run does not overwrite it.
        ArchiveImportItem.objects.filter(pr_number=2).update(
            status=ArchiveImportItemStatus.COMPLETED,
            attempts=3,
            last_error="foo",
        )
        call_command(
            "bootstrap_archive_worklist",
            "--archive",
            "queueboard-archive2",
            "--repo",
            "leanprover-community/mathlib4",
            stdout=io.StringIO(),
        )
        self.assertEqual(
            ArchiveImportItem.objects.filter(repository=self.repo, archive_name="queueboard-archive2").count(),
            3,
        )
        # The COMPLETED row was not reset back to PENDING by the re-run.
        row2 = ArchiveImportItem.objects.get(pr_number=2)
        self.assertEqual(row2.status, ArchiveImportItemStatus.COMPLETED)
        self.assertEqual(row2.attempts, 3)
        self.assertEqual(row2.last_error, "foo")

    @mock.patch("syncer.management.commands.bootstrap_archive_worklist.enumerate_archive_pr_entries")
    def test_diff_against_skips_completed_pr_numbers_from_other_archive(self, mock_enum) -> None:
        # Pre-populate archive2: PR 1 completed, PR 2 failed_permanent, PR 3 pending.
        ArchiveImportItem.objects.bulk_create(
            [
                ArchiveImportItem(
                    repository=self.repo,
                    archive_name="queueboard-archive2",
                    pr_number=1,
                    archive_path="data/1/pr_info.json",
                    status=ArchiveImportItemStatus.COMPLETED,
                ),
                ArchiveImportItem(
                    repository=self.repo,
                    archive_name="queueboard-archive2",
                    pr_number=2,
                    archive_path="data/2/pr_info.json",
                    status=ArchiveImportItemStatus.FAILED_PERMANENT,
                ),
                ArchiveImportItem(
                    repository=self.repo,
                    archive_name="queueboard-archive2",
                    pr_number=3,
                    archive_path="data/3/pr_info.json",
                    status=ArchiveImportItemStatus.PENDING,
                ),
            ]
        )

        # Older archive enumerates PR 1, 2, 3, 4.
        mock_enum.return_value = _entries(1, 2, 3, 4)
        call_command(
            "bootstrap_archive_worklist",
            "--archive",
            "queueboard-archive",
            "--repo",
            "leanprover-community/mathlib4",
            "--diff-against",
            "queueboard-archive2",
            stdout=io.StringIO(),
        )

        archive_rows = ArchiveImportItem.objects.filter(repository=self.repo, archive_name="queueboard-archive").order_by(
            "pr_number"
        )
        # PR 1 was completed in archive2, so it must NOT be enrolled in the older archive.
        # PRs 2, 3, 4 must be enrolled (not completed elsewhere).
        self.assertEqual([r.pr_number for r in archive_rows], [2, 3, 4])
        for row in archive_rows:
            self.assertEqual(row.status, ArchiveImportItemStatus.PENDING)

    @mock.patch("syncer.management.commands.bootstrap_archive_worklist.enumerate_archive_pr_entries")
    def test_limit_caps_enrollment(self, mock_enum) -> None:
        mock_enum.return_value = _entries(1, 2, 3, 4, 5)
        call_command(
            "bootstrap_archive_worklist",
            "--archive",
            "queueboard-archive2",
            "--repo",
            "leanprover-community/mathlib4",
            "--limit",
            "2",
            stdout=io.StringIO(),
        )
        self.assertEqual(
            list(
                ArchiveImportItem.objects.filter(repository=self.repo, archive_name="queueboard-archive2")
                .order_by("pr_number")
                .values_list("pr_number", flat=True)
            ),
            [1, 2],
        )
