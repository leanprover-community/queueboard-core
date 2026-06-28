"""Tests for the read-only `audit_revision_contiguity` command (design decision 049)."""

from __future__ import annotations

from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from analyzer.management.commands.audit_revision_contiguity import classify_revision_windows
from analyzer.models import PRRevision
from core.models import Repository
from syncer.models import PullRequest


class TestClassifyRevisionWindows(SimpleTestCase):
    """Pure-logic coverage (no database)."""

    def _w(self, *pairs):
        # pairs are (from_min, to_min|None) integer minutes for readability
        base = timezone.now()
        return [(base + timedelta(minutes=f), None if t is None else base + timedelta(minutes=t)) for f, t in pairs]

    def test_empty_is_clean(self) -> None:
        self.assertEqual(classify_revision_windows([]), set())

    def test_single_open_is_clean(self) -> None:
        self.assertEqual(classify_revision_windows(self._w((0, None))), set())

    def test_contiguous_is_clean(self) -> None:
        self.assertEqual(classify_revision_windows(self._w((0, 30), (30, None))), set())

    def test_gap(self) -> None:
        self.assertEqual(classify_revision_windows(self._w((0, 30), (60, None))), {"gap"})

    def test_overlap(self) -> None:
        self.assertEqual(classify_revision_windows(self._w((0, 90), (60, None))), {"overlap"})

    def test_backward(self) -> None:
        # A lone backward window (to_ts < from_ts) with no neighbour to also gap against.
        self.assertEqual(classify_revision_windows(self._w((30, 10))), {"backward"})

    def test_open_mid(self) -> None:
        self.assertEqual(classify_revision_windows(self._w((0, None), (30, None))), {"open_mid"})

    def test_multiple_categories(self) -> None:
        # gap between 0->30 and 60, then a backward final window.
        self.assertEqual(classify_revision_windows(self._w((0, 30), (60, 50))), {"gap", "backward"})


class TestAuditRevisionContiguityCommand(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="o", name="r", default_branch="master", is_active=True)
        self.t0 = timezone.now() - timedelta(days=1)

    def _at(self, minutes: int):
        return self.t0 + timedelta(minutes=minutes)

    def _pr(self, number: int) -> PullRequest:
        return PullRequest.objects.create(
            repository=self.repo,
            number=number,
            author=None,
            state="open",
            is_draft=False,
            gh_created_at=self._at(0),
            gh_updated_at=self._at(0),
            base_ref_name="master",
            head_ref_name="b",
            head_repo_owner_login="o",
            head_repo_name="fork",
            title="t",
            body="",
            additions=0,
            deletions=0,
            changed_files_count=0,
            timeline_backfill_done=True,
        )

    def _rev(self, pr, head, from_min, to_min, seq):
        PRRevision.objects.create(
            pull_request=pr,
            head_sha=head,
            from_ts=self._at(from_min),
            to_ts=None if to_min is None else self._at(to_min),
            seq=seq,
        )

    def test_reports_gap_and_clean_prs(self) -> None:
        # Contiguous PR — must not be reported.
        clean = self._pr(1)
        self._rev(clean, "a", 0, 30, 0)
        self._rev(clean, "b", 30, None, 1)
        # Gappy PR — must be reported.
        gappy = self._pr(2)
        self._rev(gappy, "a", 0, 30, 0)
        self._rev(gappy, "b", 60, None, 1)

        out = StringIO()
        call_command("audit_revision_contiguity", "--repo", "o/r", stdout=out)
        text = out.getvalue()

        self.assertIn("1/2 PRs violate contiguity", text)
        self.assertIn("gap=1", text)
        self.assertIn("PR #2", text)
        self.assertNotIn("PR #1", text)
