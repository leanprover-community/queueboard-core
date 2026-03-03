from __future__ import annotations

import io
import json

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from syncer.models import CheckRun, CommitCheckRun
from syncer.tests.factories import make_pr, make_repo


class TestBackfillShaKeyedCiCommand(TestCase):
    def test_dry_run_does_not_persist_rows(self) -> None:
        repo = make_repo(owner="leanprover-community", name="mathlib4")
        pr = make_pr(repo, 1, head_sha="a" * 40)
        CheckRun.objects.create(
            pull_request=pr,
            github_node_id="CR_CMD_DRY",
            head_sha="a" * 40,
            name="build",
            status="COMPLETED",
            conclusion="SUCCESS",
            gh_completed_at=timezone.now(),
        )

        out = io.StringIO()
        call_command(
            "backfill_sha_keyed_ci",
            "--repo",
            "leanprover-community/mathlib4",
            "--max-checkruns",
            "10",
            "--max-status-contexts",
            "0",
            "--dry-run",
            stdout=out,
        )
        self.assertEqual(CommitCheckRun.objects.count(), 0)
        self.assertIn("Planned rows: total=1", out.getvalue())
        self.assertIn("SHA-keyed CI backfill (DRY-RUN)", out.getvalue())

    def test_apply_outputs_json_summary(self) -> None:
        repo = make_repo(owner="leanprover-community", name="mathlib4")
        pr = make_pr(repo, 1, head_sha="a" * 40)
        CheckRun.objects.create(
            pull_request=pr,
            github_node_id="CR_CMD_APPLY",
            head_sha="a" * 40,
            name="build",
            status="COMPLETED",
            conclusion="SUCCESS",
            gh_completed_at=timezone.now(),
        )

        out = io.StringIO()
        call_command(
            "backfill_sha_keyed_ci",
            "--repo",
            "leanprover-community/mathlib4",
            "--max-checkruns",
            "10",
            "--max-status-contexts",
            "0",
            stdout=out,
        )

        self.assertEqual(CommitCheckRun.objects.count(), 1)
        text = out.getvalue()
        self.assertIn("Planned rows: total=1", text)
        payload = json.loads(text[text.index("{") :])
        self.assertEqual(payload["check_runs"]["inserted"], 1)

    def test_prints_progress_every_1000_rows(self) -> None:
        repo = make_repo(owner="leanprover-community", name="mathlib4")
        pr = make_pr(repo, 1, head_sha="a" * 40)
        now = timezone.now()
        rows = [
            CheckRun(
                pull_request=pr,
                github_node_id=f"CR_CMD_PROGRESS_{i}",
                head_sha="a" * 40,
                name="build",
                status="COMPLETED",
                conclusion="SUCCESS",
                gh_completed_at=now,
            )
            for i in range(1001)
        ]
        CheckRun.objects.bulk_create(rows)

        out = io.StringIO()
        call_command(
            "backfill_sha_keyed_ci",
            "--repo",
            "leanprover-community/mathlib4",
            "--batch-size",
            "250",
            "--max-checkruns",
            "1001",
            "--max-status-contexts",
            "0",
            stdout=out,
        )
        text = out.getvalue()
        self.assertIn("Planned rows: total=1001", text)
        self.assertIn("Progress: total=1000/1001", text)
        self.assertIn("check_runs=1000/1001", text)
        self.assertIn("status_contexts=0/0", text)
