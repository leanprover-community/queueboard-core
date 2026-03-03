from __future__ import annotations

from django.test import TestCase
from django.utils import timezone

from syncer.models import CheckRun, CommitCheckRun, CommitStatusContext, StatusContext
from syncer.services.ci_storage_backfill import backfill_commit_ci_rows
from syncer.tests.factories import make_pr, make_repo


class TestCIStorageBackfill(TestCase):
    def setUp(self) -> None:
        self.repo = make_repo(owner="leanprover-community", name="mathlib4")
        self.pr = make_pr(self.repo, 1, head_sha="a" * 40)

    def test_backfill_inserts_and_is_idempotent(self) -> None:
        now = timezone.now()
        CheckRun.objects.create(
            pull_request=self.pr,
            github_node_id="CR_BF_1",
            head_sha="a" * 40,
            name="build",
            status="COMPLETED",
            conclusion="SUCCESS",
            gh_completed_at=now,
            last_synced_at=now,
        )
        StatusContext.objects.create(
            pull_request=self.pr,
            github_node_id="SC_BF_1",
            rest_id=11,
            head_sha="a" * 40,
            name="bors",
            state="SUCCESS",
            gh_created_at=now,
            last_synced_at=now,
        )

        first = backfill_commit_ci_rows(batch_size=10)
        self.assertEqual(first.check_runs.inserted, 1)
        self.assertEqual(first.status_contexts.inserted, 1)
        self.assertEqual(CommitCheckRun.objects.count(), 1)
        self.assertEqual(CommitStatusContext.objects.count(), 1)

        second = backfill_commit_ci_rows(batch_size=10)
        self.assertEqual(second.check_runs.inserted, 0)
        self.assertEqual(second.status_contexts.inserted, 0)
        self.assertEqual(second.check_runs.skipped_duplicate, 1)
        self.assertEqual(second.status_contexts.skipped_duplicate, 1)
        self.assertEqual(CommitCheckRun.objects.count(), 1)
        self.assertEqual(CommitStatusContext.objects.count(), 1)

    def test_backfill_updates_existing_commit_rows(self) -> None:
        now = timezone.now()
        CheckRun.objects.create(
            pull_request=self.pr,
            github_node_id="CR_BF_UPD",
            head_sha="a" * 40,
            name="build",
            status="COMPLETED",
            conclusion="SUCCESS",
            gh_completed_at=now,
            last_synced_at=now,
        )
        CommitCheckRun.objects.create(
            repository=self.repo,
            github_node_id="CR_BF_UPD",
            head_sha="a" * 40,
            name="build",
            status="IN_PROGRESS",
            conclusion=None,
        )

        stats = backfill_commit_ci_rows(batch_size=10)
        self.assertEqual(stats.check_runs.updated, 1)
        ckr = CommitCheckRun.objects.get(github_node_id="CR_BF_UPD")
        self.assertEqual(ckr.status, "COMPLETED")
        self.assertEqual(ckr.conclusion, "SUCCESS")

    def test_status_context_without_provider_id_is_skipped_invalid(self) -> None:
        StatusContext.objects.create(
            pull_request=self.pr,
            github_node_id=None,
            rest_id=None,
            head_sha="a" * 40,
            name="legacy",
            state="SUCCESS",
            gh_created_at=timezone.now(),
        )

        stats = backfill_commit_ci_rows(batch_size=10)
        self.assertEqual(stats.status_contexts.scanned, 1)
        self.assertEqual(stats.status_contexts.skipped_invalid, 1)
        self.assertEqual(CommitStatusContext.objects.count(), 0)

    def test_resume_cursor_and_repo_filter(self) -> None:
        other_repo = make_repo(owner="other", name="repo")
        other_pr = make_pr(other_repo, 2, head_sha="b" * 40)
        now = timezone.now()
        cr1 = CheckRun.objects.create(
            pull_request=self.pr,
            github_node_id="CR_RESUME_1",
            head_sha="a" * 40,
            name="build",
            status="COMPLETED",
            conclusion="SUCCESS",
            gh_completed_at=now,
        )
        CheckRun.objects.create(
            pull_request=self.pr,
            github_node_id="CR_RESUME_2",
            head_sha="a" * 40,
            name="lint",
            status="COMPLETED",
            conclusion="SUCCESS",
            gh_completed_at=now,
        )
        CheckRun.objects.create(
            pull_request=other_pr,
            github_node_id="CR_OTHER_REPO",
            head_sha="b" * 40,
            name="build",
            status="COMPLETED",
            conclusion="SUCCESS",
            gh_completed_at=now,
        )

        stats = backfill_commit_ci_rows(
            repo_id=self.repo.id,
            checkrun_start_id=cr1.id,
            max_checkruns=10,
            max_status_contexts=0,
            batch_size=10,
        )
        self.assertEqual(stats.check_runs.scanned, 1)
        self.assertEqual(stats.check_runs.inserted, 1)
        self.assertEqual(CommitCheckRun.objects.filter(repository=self.repo).count(), 1)
        self.assertEqual(CommitCheckRun.objects.filter(repository=other_repo).count(), 0)
