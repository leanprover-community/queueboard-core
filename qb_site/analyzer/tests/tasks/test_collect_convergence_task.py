from __future__ import annotations

from django.test import TestCase
from django.utils import timezone

from analyzer.models import AnalyzerConvergenceSnapshot, PRQueueWindow, PRRevision, PRRevisionBuildState, QueueRuleSet
from analyzer.tasks.collect_convergence import collect_analyzer_convergence_task
from core.models import Repository
from syncer.models import PullRequest


class TestCollectAnalyzerConvergenceTask(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="o", name="r", default_branch="master", is_active=True)
        self.rule_set = QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            require_open=True,
            require_not_draft=True,
            require_ci_success=True,
        )

    def _mk_pr(self, number: int, timeline_done: bool = True) -> PullRequest:
        now = timezone.now()
        return PullRequest.objects.create(
            repository=self.repo,
            number=number,
            timeline_backfill_done=timeline_done,
            commits_backfill_done=timeline_done,
            state="open",
            is_draft=False,
            gh_created_at=now,
            gh_updated_at=now,
            base_ref_name="master",
            head_ref_name="branch",
            head_repo_owner_login="o",
            head_repo_name="r",
            title=f"t{number}",
            body="",
            additions=0,
            deletions=0,
            changed_files_count=0,
        )

    def test_collects_convergence_counts(self) -> None:
        # PR with no revisions
        pr1 = self._mk_pr(1)
        pr1.engagement_synced_at = timezone.now()
        pr1.save(update_fields=["engagement_synced_at"])
        # PR with revisions but stale windows/ci check and incomplete engagement
        pr2 = self._mk_pr(2)
        pr2.files_incomplete = True
        pr2.save(update_fields=["files_incomplete"])
        PRRevision.objects.create(pull_request=pr2, head_sha="a1", from_ts=pr2.gh_created_at, to_ts=None, seq=0)
        PRRevisionBuildState.objects.create(
            pull_request=pr2,
            revision_version=2,
            ci_checked_revision_version=None,
            windows_built_revision_version=1,
            windows_built_at=pr2.gh_created_at,
        )
        # PR missing timeline/commits backfill
        pr3 = self._mk_pr(3, timeline_done=False)
        PRQueueWindow.objects.create(
            pull_request=pr2,
            rule_set=self.rule_set,
            from_ts=pr2.gh_created_at,
            to_ts=None,
            cycle_index=0,
            window_count=0,
            first_on_queue_ts=None,
        )

        res = collect_analyzer_convergence_task.apply().get()
        snap = AnalyzerConvergenceSnapshot.objects.filter(repository=self.repo).order_by("-collected_at").first()
        self.assertIsNotNone(snap)
        self.assertEqual(snap.pr_no_revisions, 1)
        self.assertEqual(snap.windows_stale, 1)
        self.assertEqual(snap.ci_not_checked, 1)
        # CI-gated missing windows only counts PRs with revisions; pr2 has a window row.
        self.assertEqual(snap.ci_gated_missing_windows, 0)
        self.assertEqual(snap.prs_missing_dependency_state, 2)
        self.assertEqual(snap.prs_stale_dependency_state, 0)
        self.assertEqual(snap.prs_missing_queue_window_rollups, 1)
        self.assertEqual(res["rows_created"], 1)

    def test_windows_stale_clears_after_sweep_updates_built_at(self) -> None:
        pr = self._mk_pr(10)
        PRRevision.objects.create(pull_request=pr, head_sha="a1", from_ts=pr.gh_created_at, to_ts=None, seq=0)
        old_built_at = timezone.now() - timezone.timedelta(days=3)
        PRRevisionBuildState.objects.create(
            pull_request=pr,
            revision_version=1,
            windows_built_revision_version=1,
            windows_built_at=old_built_at,
        )
        PRQueueWindow.objects.create(
            pull_request=pr,
            rule_set=self.rule_set,
            from_ts=pr.gh_created_at,
            to_ts=None,
            cycle_index=0,
            window_count=1,
            first_on_queue_ts=pr.gh_created_at,
        )
        # Bump ruleset timestamp to mark PR stale in convergence.
        self.rule_set.updated_at = timezone.now()
        self.rule_set.save(update_fields=["updated_at"])

        res_before = collect_analyzer_convergence_task.apply().get()
        snap_before = AnalyzerConvergenceSnapshot.objects.filter(repository=self.repo).order_by("-collected_at").first()
        self.assertIsNotNone(snap_before)
        self.assertEqual(snap_before.windows_stale, 1)
        self.assertEqual(res_before["rows_created"], 1)

        from analyzer.tasks.rebuild_queue_windows_sweep import rebuild_queue_windows_sweep_task

        rebuild_queue_windows_sweep_task.apply(kwargs={"max_prs_per_repo": 5}).get()

        res_after = collect_analyzer_convergence_task.apply().get()
        snap_after = AnalyzerConvergenceSnapshot.objects.filter(repository=self.repo).order_by("-collected_at").first()
        self.assertIsNotNone(snap_after)
        self.assertEqual(snap_after.windows_stale, 0)
        self.assertEqual(res_after["rows_created"], 1)
