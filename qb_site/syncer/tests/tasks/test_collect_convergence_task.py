from __future__ import annotations

from django.test import TestCase
from django.utils import timezone

from core.models import Repository
from syncer.models import PullRequest, CommitHistoryHarvest, RepoBackfillCursor, RepoDiscoveryState, SyncerConvergenceSnapshot
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
        pr2 = PullRequest.objects.create(
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
        CommitHistoryHarvest.objects.create(pull_request=pr2, start_sha="a" * 40, has_more=True)
        RepoBackfillCursor.objects.create(repository=self.repo, completed=False)
        cutoff = now - timezone.timedelta(minutes=20)
        RepoDiscoveryState.objects.create(
            repository=self.repo,
            last_successful_cutoff_at=cutoff,
            continuation_cutoff_at=cutoff,
            continuation_cursor="CUR-1",
            last_attempted_at=now - timezone.timedelta(minutes=2),
            last_successful_at=now - timezone.timedelta(minutes=5),
        )

        res = collect_syncer_convergence_task.apply().get()
        snap = SyncerConvergenceSnapshot.objects.filter(repository=self.repo).order_by("-collected_at").first()
        self.assertIsNotNone(snap)
        self.assertEqual(snap.timeline_backfill_pending, 1)
        self.assertEqual(snap.commits_backfill_pending, 1)
        self.assertEqual(snap.incomplete_prs, 3)
        self.assertEqual(snap.harvest_jobs_open, 1)
        self.assertFalse(snap.history_cursor_completed)
        self.assertIsNotNone(snap.discovery_lag_seconds)
        self.assertGreaterEqual(int(snap.discovery_lag_seconds), 1200)
        self.assertTrue(snap.discovery_continuation_active)
        self.assertIsNotNone(snap.discovery_last_attempted_at)
        self.assertIsNotNone(snap.discovery_last_successful_at)
        self.assertEqual(snap.prs_missing_engagement, 2)
        self.assertEqual(snap.prs_engagement_incomplete, 1)
        self.assertEqual(snap.prs_missing_head_ci_state, 2)
        self.assertEqual(snap.prs_missing_head_sha, 1)
        self.assertEqual(res["per_repo"][0]["discovery_continuation_active"], True)
        self.assertIsNotNone(res["per_repo"][0]["discovery_lag_seconds"])
        self.assertEqual(res["rows_created"], 1)

    def test_discovery_lag_uses_current_successful_cutoff(self) -> None:
        now = timezone.now()
        # Simulate a stale/old watermark first, then a newer successful cutoff.
        state = RepoDiscoveryState.objects.create(
            repository=self.repo,
            last_successful_cutoff_at=now - timezone.timedelta(hours=3),
            last_attempted_at=now - timezone.timedelta(hours=3),
            last_successful_at=now - timezone.timedelta(hours=3),
        )

        collect_syncer_convergence_task.apply().get()
        first = SyncerConvergenceSnapshot.objects.filter(repository=self.repo).order_by("-collected_at").first()
        self.assertIsNotNone(first)
        assert first is not None
        self.assertIsNotNone(first.discovery_lag_seconds)

        # Advance watermark to a much newer cutoff as a successful fresh run would.
        state.last_successful_cutoff_at = timezone.now() - timezone.timedelta(minutes=20)
        state.last_attempted_at = timezone.now() - timezone.timedelta(minutes=1)
        state.last_successful_at = timezone.now() - timezone.timedelta(minutes=1)
        state.save(update_fields=["last_successful_cutoff_at", "last_attempted_at", "last_successful_at", "updated_at"])

        collect_syncer_convergence_task.apply().get()
        snaps = list(SyncerConvergenceSnapshot.objects.filter(repository=self.repo).order_by("-collected_at")[:2])
        self.assertEqual(len(snaps), 2)
        newer, older = snaps[0], snaps[1]
        self.assertIsNotNone(newer.discovery_lag_seconds)
        self.assertIsNotNone(older.discovery_lag_seconds)
        assert newer.discovery_lag_seconds is not None
        assert older.discovery_lag_seconds is not None
        self.assertLess(newer.discovery_lag_seconds, older.discovery_lag_seconds)
