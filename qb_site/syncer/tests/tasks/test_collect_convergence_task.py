from __future__ import annotations

from django.test import TestCase
from django.utils import timezone

from core.models import Repository
from syncer.models import PullRequest, CommitHistoryHarvest, RepoBackfillCursor, SyncerConvergenceSnapshot
from syncer.tasks.collect_convergence import collect_syncer_convergence_task


class TestCollectConvergenceTask(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="o", name="r", default_branch="master", is_active=True)

    def test_collects_counts(self) -> None:
        now = timezone.now()
        # PRs with pending backfill
        PullRequest.objects.create(
            repository=self.repo,
            number=1,
            timeline_backfill_done=False,
            commits_backfill_done=True,
            files_incomplete=True,
            head_sha=None,
            state="open",
            gh_created_at=now,
            gh_updated_at=now,
            base_ref_name="master",
            head_ref_name="branch",
            head_repo_owner_login="o",
            head_repo_name="r",
            title="t",
            body="",
            additions=0,
            deletions=0,
            changed_files_count=0,
        )
        PullRequest.objects.create(
            repository=self.repo,
            number=2,
            timeline_backfill_done=True,
            commits_backfill_done=False,
            engagement_synced_at=None,
            head_sha="a" * 40,
            state="open",
            gh_created_at=now,
            gh_updated_at=now,
            base_ref_name="master",
            head_ref_name="branch",
            head_repo_owner_login="o",
            head_repo_name="r",
            title="t2",
            body="",
            additions=0,
            deletions=0,
            changed_files_count=0,
        )
        PullRequest.objects.create(
            repository=self.repo,
            number=3,
            timeline_backfill_done=True,
            commits_backfill_done=True,
            head_ci_state="PENDING",
            engagement_synced_at=now,
            head_sha="b" * 40,
            state="open",
            gh_created_at=now,
            gh_updated_at=now,
            base_ref_name="master",
            head_ref_name="branch",
            head_repo_owner_login="o",
            head_repo_name="r",
            title="t3",
            body="",
            additions=0,
            deletions=0,
            changed_files_count=0,
        )
        CommitHistoryHarvest.objects.create(pull_request_id=2, start_sha="a" * 40, has_more=True)
        RepoBackfillCursor.objects.create(repository=self.repo, completed=False)

        res = collect_syncer_convergence_task.apply().get()
        snap = SyncerConvergenceSnapshot.objects.filter(repository=self.repo).order_by("-collected_at").first()
        self.assertIsNotNone(snap)
        self.assertEqual(snap.timeline_backfill_pending, 1)
        self.assertEqual(snap.commits_backfill_pending, 1)
        self.assertEqual(snap.incomplete_prs, 3)
        self.assertEqual(snap.harvest_jobs_open, 1)
        self.assertFalse(snap.history_cursor_completed)
        self.assertEqual(snap.prs_missing_engagement, 2)
        self.assertEqual(snap.prs_engagement_incomplete, 1)
        self.assertEqual(snap.prs_missing_head_ci_state, 2)
        self.assertEqual(snap.prs_missing_head_sha, 1)
        self.assertEqual(res["rows_created"], 1)
