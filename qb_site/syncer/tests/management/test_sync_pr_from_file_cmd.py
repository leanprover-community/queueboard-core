from __future__ import annotations

from django.core.management import call_command
from django.test import TestCase

from syncer.models import (
    PullRequest,
    LabelDef,
    PRLabel,
    PRTimelineEvent,
    CheckRun,
    CommitCheckRun,
    CommitStatusContext,
    StatusContext,
)
from syncer.tests.factories import make_repo
from syncer.tests.helpers import fixtures_dir


class TestSyncFromFileCommand(TestCase):
    def setUp(self) -> None:
        # Pre-create repository to avoid side effects outside transaction
        self.repo = make_repo(owner="leanprover-community", name="mathlib4")
        self.fixture = fixtures_dir() / "pr_bundle_min.json"

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
        self.assertEqual(CheckRun.objects.filter(pull_request=pr).count(), 0)
        self.assertEqual(StatusContext.objects.filter(pull_request=pr).count(), 0)
        self.assertEqual(CommitCheckRun.objects.filter(repository=self.repo).count(), 1)
        self.assertEqual(CommitStatusContext.objects.filter(repository=self.repo).count(), 1)

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
        self.assertEqual(CheckRun.objects.filter(pull_request=pr).count(), 0)
        self.assertEqual(StatusContext.objects.filter(pull_request=pr).count(), 0)
        self.assertEqual(CommitCheckRun.objects.filter(repository=self.repo).count(), 1)
        self.assertEqual(CommitStatusContext.objects.filter(repository=self.repo).count(), 1)
