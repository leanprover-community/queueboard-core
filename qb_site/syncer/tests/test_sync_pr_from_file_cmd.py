from __future__ import annotations

from pathlib import Path

from django.core.management import call_command
from django.test import TestCase

from core.models.repository import Repository
from syncer.models import PullRequest, LabelDef, PRLabel, PRTimelineEvent, CheckRun, StatusContext


class TestSyncFromFileCommand(TestCase):
    def setUp(self) -> None:
        # Pre-create repository to avoid side effects outside transaction
        self.repo = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master", is_active=True)
        self.fixture = Path(__file__).resolve().parent / "fixtures" / "pr_bundle_min.json"

    def test_dry_run_no_changes(self) -> None:
        # Ensure empty state
        self.assertEqual(PullRequest.objects.count(), 0)
        # Dry-run should not persist DB writes
        call_command(
            "sync_pr_from_file",
            "--repo",
            "leanprover-community/mathlib4",
            "--file",
            str(self.fixture),
            "--dry-run",
        )
        # No rows created
        self.assertEqual(PullRequest.objects.count(), 0)

    def test_apply_creates_rows_and_idempotent(self) -> None:
        call_command(
            "sync_pr_from_file",
            "--repo",
            "leanprover-community/mathlib4",
            "--file",
            str(self.fixture),
        )
        pr = PullRequest.objects.get(repository=self.repo, number=30723)
        # Repository node id persisted
        self.repo.refresh_from_db()
        self.assertEqual(self.repo.github_node_id, "R_repo123")
        # Default branch updated from bundle
        self.assertEqual(self.repo.default_branch, "main")
        # Author node id + metadata persisted
        self.assertIsNotNone(pr.author)
        self.assertEqual(pr.author.github_node_id, "U_user123")
        self.assertEqual(pr.author.github_login, "test-author")
        # Labels
        self.assertGreaterEqual(LabelDef.objects.filter(repository=self.repo).count(), 2)
        self.assertGreaterEqual(PRLabel.objects.filter(pull_request=pr).count(), 2)
        # Timeline
        self.assertEqual(PRTimelineEvent.objects.filter(pull_request=pr).count(), 4)
        # CI snapshots
        self.assertEqual(CheckRun.objects.filter(pull_request=pr).count(), 1)
        self.assertEqual(StatusContext.objects.filter(pull_request=pr).count(), 1)

        # Idempotent second run
        call_command(
            "sync_pr_from_file",
            "--repo",
            "leanprover-community/mathlib4",
            "--file",
            str(self.fixture),
        )
        self.assertEqual(PRLabel.objects.filter(pull_request=pr).count(), 2)
        self.assertEqual(PRTimelineEvent.objects.filter(pull_request=pr).count(), 4)
        self.assertEqual(CheckRun.objects.filter(pull_request=pr).count(), 1)
        self.assertEqual(StatusContext.objects.filter(pull_request=pr).count(), 1)
