from __future__ import annotations

from django.test import TestCase
from django.utils import timezone

from core.models import Repository
from syncer.models import (
    PullRequest,
    CommitCheckRun,
    CommitHistoryHarvest,
    RepoBackfillCursor,
    RepoDiscoveryState,
    SyncerConvergenceSnapshot,
)
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
        self.assertEqual(snap.prs_missing_head_ci_contexts, 2)
        # All three PRs are at sync_schema_version=0 (default) and CURRENT=2,
        # so the wave-progress canary reports 3 below target.
        self.assertEqual(snap.prs_below_current_sync_schema_version, 3)
        self.assertEqual(snap.sync_schema_version_target, 2)
        self.assertEqual(res["per_repo"][0]["discovery_continuation_active"], True)
        self.assertIsNotNone(res["per_repo"][0]["discovery_lag_seconds"])
        self.assertEqual(res["per_repo"][0]["prs_below_current_sync_schema_version"], 3)
        self.assertEqual(res["per_repo"][0]["sync_schema_version_target"], 2)
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

    def test_discovery_catchup_lag_populated_when_continuation_has_success_cutoff(self) -> None:
        now = timezone.now()
        watermark = now - timezone.timedelta(days=10)
        target = now - timezone.timedelta(minutes=30)
        RepoDiscoveryState.objects.create(
            repository=self.repo,
            last_successful_cutoff_at=watermark,
            continuation_cutoff_at=watermark,
            continuation_cursor="CUR-CATCH",
            continuation_success_cutoff=target,
            last_attempted_at=now - timezone.timedelta(minutes=1),
            last_successful_at=now - timezone.timedelta(days=10),
        )

        collect_syncer_convergence_task.apply().get()
        snap = SyncerConvergenceSnapshot.objects.filter(repository=self.repo).order_by("-collected_at").first()
        self.assertIsNotNone(snap)
        assert snap is not None
        expected = int((target - watermark).total_seconds())
        self.assertIsNotNone(snap.discovery_catchup_lag_seconds)
        assert snap.discovery_catchup_lag_seconds is not None
        self.assertAlmostEqual(snap.discovery_catchup_lag_seconds, expected, delta=5)

    def test_discovery_catchup_lag_null_when_no_continuation_success_cutoff(self) -> None:
        now = timezone.now()
        RepoDiscoveryState.objects.create(
            repository=self.repo,
            last_successful_cutoff_at=now - timezone.timedelta(minutes=30),
            last_attempted_at=now - timezone.timedelta(minutes=1),
            last_successful_at=now - timezone.timedelta(minutes=1),
        )

        collect_syncer_convergence_task.apply().get()
        snap = SyncerConvergenceSnapshot.objects.filter(repository=self.repo).order_by("-collected_at").first()
        self.assertIsNotNone(snap)
        assert snap is not None
        self.assertIsNone(snap.discovery_catchup_lag_seconds)

    def test_head_contexts_include_commit_scoped_rows(self) -> None:
        now = timezone.now()
        pr_with_commit_ci = PullRequest.objects.create(
            repository=self.repo,
            number=100,
            timeline_backfill_done=True,
            commits_backfill_done=True,
            head_sha="sha_commit",
            state="open",
            gh_created_at=now,
            gh_updated_at=now,
            base_ref_name="master",
            head_ref_name="branch",
            head_repo_owner_login="o",
            head_repo_name="r",
            title="t100",
            body="",
            additions=0,
            deletions=0,
            changed_files_count=0,
        )
        PullRequest.objects.create(
            repository=self.repo,
            number=101,
            timeline_backfill_done=True,
            commits_backfill_done=True,
            head_sha="sha_missing",
            state="open",
            gh_created_at=now,
            gh_updated_at=now,
            base_ref_name="master",
            head_ref_name="branch",
            head_repo_owner_login="o",
            head_repo_name="r",
            title="t101",
            body="",
            additions=0,
            deletions=0,
            changed_files_count=0,
        )
        CommitCheckRun.objects.create(
            repository=self.repo,
            github_node_id="CCR_HEAD",
            head_sha=pr_with_commit_ci.head_sha,
            name="ci/test",
            status="COMPLETED",
            conclusion="SUCCESS",
            gh_started_at=now,
            gh_completed_at=now,
        )

        collect_syncer_convergence_task.apply().get()
        snap = SyncerConvergenceSnapshot.objects.filter(repository=self.repo).order_by("-collected_at").first()
        self.assertIsNotNone(snap)
        self.assertEqual(snap.prs_missing_head_ci_contexts, 1)

    def test_sync_schema_version_target_reflects_module_constant(self) -> None:
        # Mix of PRs at v=0 (below target), v=1 (at-or-above target depending on
        # the patched CURRENT). Patch CURRENT=2 inside the convergence task's
        # namespace to verify the metric tracks the *current* target rather
        # than a stale historical value.
        now = timezone.now()
        for n in (1, 2):
            PullRequest.objects.create(
                repository=self.repo,
                number=n,
                state="open",
                gh_created_at=now,
                gh_updated_at=now,
                base_ref_name="master",
                head_ref_name="branch",
                head_repo_owner_login="o",
                head_repo_name="r",
                title=f"t{n}",
                body="",
                additions=0,
                deletions=0,
                changed_files_count=0,
                sync_schema_version=0,
            )
        for n in (3, 4):
            PullRequest.objects.create(
                repository=self.repo,
                number=n,
                state="open",
                gh_created_at=now,
                gh_updated_at=now,
                base_ref_name="master",
                head_ref_name="branch",
                head_repo_owner_login="o",
                head_repo_name="r",
                title=f"t{n}",
                body="",
                additions=0,
                deletions=0,
                changed_files_count=0,
                sync_schema_version=1,
            )

        from unittest import mock as _mock

        with _mock.patch("syncer.tasks.collect_convergence.CURRENT_SYNC_SCHEMA_VERSION", 2):
            collect_syncer_convergence_task.apply().get()
        snap = SyncerConvergenceSnapshot.objects.filter(repository=self.repo).order_by("-collected_at").first()
        self.assertIsNotNone(snap)
        assert snap is not None
        # 2 PRs at v=0 + 2 PRs at v=1, all below CURRENT=2 → 4 below target.
        self.assertEqual(snap.prs_below_current_sync_schema_version, 4)
        self.assertEqual(snap.sync_schema_version_target, 2)
