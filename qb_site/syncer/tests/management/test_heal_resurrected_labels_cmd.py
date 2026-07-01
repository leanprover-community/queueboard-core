"""Tests for the heal_resurrected_labels command."""

from __future__ import annotations

import io
from datetime import datetime, timezone as _tz

from django.core.management import CommandError, call_command
from django.test import TestCase

from syncer.models import LabelDef, PRLabel, PRTimelineEvent, PRTimelineEventType
from syncer.tests.factories import make_pr, make_repo


class TestHealResurrectedLabelsCommand(TestCase):
    def setUp(self) -> None:
        self.repo = make_repo(owner="leanprover-community", name="mathlib4")
        self.pr = make_pr(self.repo, 42, state="closed")
        self.label = LabelDef.objects.create(repository=self.repo, name="maintainer-merge", color="ededed")
        self.prlabel = PRLabel.objects.create(pull_request=self.pr, label_def=self.label)
        PRTimelineEvent.objects.create(
            pull_request=self.pr,
            type=PRTimelineEventType.UNLABELED,
            occurred_at=datetime(2026, 3, 11, tzinfo=_tz.utc),
            label_name="maintainer-merge",
        )

    def test_dry_run_reports_but_does_not_delete(self) -> None:
        out = io.StringIO()
        call_command("heal_resurrected_labels", stdout=out)
        output = out.getvalue()
        self.assertIn("mathlib4#42", output)
        self.assertIn("maintainer-merge", output)
        self.assertIn("dry-run", output)
        self.assertTrue(PRLabel.objects.filter(id=self.prlabel.id).exists())

    def test_apply_deletes_stale_rows(self) -> None:
        out = io.StringIO()
        call_command("heal_resurrected_labels", "--apply", stdout=out)
        self.assertIn("Deleted 1", out.getvalue())
        self.assertFalse(PRLabel.objects.filter(id=self.prlabel.id).exists())

    def test_healthy_row_is_left_untouched(self) -> None:
        # Latest event is LABELED -> not resurrected.
        PRTimelineEvent.objects.create(
            pull_request=self.pr,
            type=PRTimelineEventType.LABELED,
            occurred_at=datetime(2026, 4, 1, tzinfo=_tz.utc),
            label_name="maintainer-merge",
        )
        out = io.StringIO()
        call_command("heal_resurrected_labels", "--apply", stdout=out)
        self.assertIn("No resurrected label attachments found.", out.getvalue())
        self.assertTrue(PRLabel.objects.filter(id=self.prlabel.id).exists())

    def test_repo_filter_validates_format(self) -> None:
        with self.assertRaises(CommandError):
            call_command("heal_resurrected_labels", "--repo", "no-slash")

    def test_unknown_repo_raises(self) -> None:
        with self.assertRaises(CommandError):
            call_command("heal_resurrected_labels", "--repo", "ghost/repo")
