from __future__ import annotations

from django.test import TestCase
from django.utils import timezone

from analyzer.models import (
    AnalyzerConvergenceSnapshot,
    PRQueueWindow,
    PRQueueWindowBuildState,
    PRRevision,
    PRRevisionBuildState,
    QueueRuleSet,
)
from analyzer.tasks.collect_convergence import collect_analyzer_convergence_task
from core.models import Repository
from syncer.models import CheckRun, CIShaFetchState, PullRequest


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
            ci_checked_revision_version=2,
            windows_built_revision_version=1,
            windows_built_at=pr2.gh_created_at,
        )
        # PR with revisions and a CI-by-SHA fetch state (should not count as "not checked").
        pr4 = self._mk_pr(4)
        PRRevision.objects.create(pull_request=pr4, head_sha="b1", from_ts=pr4.gh_created_at, to_ts=None, seq=0)
        CIShaFetchState.objects.create(
            repository=self.repo,
            sha="b1",
            last_attempted_at=timezone.now(),
            last_result="empty",
            attempts=1,
        )
        PRQueueWindow.objects.create(
            pull_request=pr4,
            rule_set=self.rule_set,
            from_ts=pr4.gh_created_at,
            to_ts=None,
            cycle_index=0,
            window_count=1,
            first_on_queue_ts=pr4.gh_created_at,
        )
        # PR with revisions and existing CI rows (should not count as "not checked").
        pr5 = self._mk_pr(5)
        PRRevision.objects.create(pull_request=pr5, head_sha="c1", from_ts=pr5.gh_created_at, to_ts=None, seq=0)
        CheckRun.objects.create(
            pull_request=pr5,
            github_node_id="cr-1",
            head_sha="c1",
            name="ci",
            status="COMPLETED",
            conclusion="SUCCESS",
        )
        PRQueueWindow.objects.create(
            pull_request=pr5,
            rule_set=self.rule_set,
            from_ts=pr5.gh_created_at,
            to_ts=None,
            cycle_index=0,
            window_count=1,
            first_on_queue_ts=pr5.gh_created_at,
        )
        # PR missing timeline/commits backfill
        self._mk_pr(3, timeline_done=False)
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
        self.assertEqual(snap.prs_missing_dependency_state, 4)
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

    def test_windows_stale_counts_per_ruleset_pairs(self) -> None:
        rs_two = QueueRuleSet.objects.create(
            repository=self.repo,
            version=2,
            require_open=True,
            require_not_draft=True,
            require_ci_success=False,
            is_active=True,
        )
        pr = self._mk_pr(11)
        PRRevision.objects.create(pull_request=pr, head_sha="z1", from_ts=pr.gh_created_at, to_ts=None, seq=0)
        built_at = timezone.now() - timezone.timedelta(days=2)
        PRRevisionBuildState.objects.create(
            pull_request=pr,
            revision_version=1,
            windows_built_revision_version=1,
            windows_built_at=built_at,
        )
        QueueRuleSet.objects.filter(pk=self.rule_set.pk).update(updated_at=built_at)
        QueueRuleSet.objects.filter(pk=rs_two.pk).update(updated_at=timezone.now())
        PRQueueWindowBuildState.objects.create(
            pull_request=pr,
            rule_set=self.rule_set,
            revision_version_built=1,
            windows_built_at=built_at,
            last_status="rebuilt",
        )
        PRQueueWindowBuildState.objects.create(
            pull_request=pr,
            rule_set=rs_two,
            revision_version_built=1,
            windows_built_at=built_at,
            last_status="rebuilt",
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
        PRQueueWindow.objects.create(
            pull_request=pr,
            rule_set=rs_two,
            from_ts=pr.gh_created_at,
            to_ts=None,
            cycle_index=0,
            window_count=1,
            first_on_queue_ts=pr.gh_created_at,
        )

        collect_analyzer_convergence_task.apply().get()
        snap = AnalyzerConvergenceSnapshot.objects.filter(repository=self.repo).order_by("-collected_at").first()
        self.assertIsNotNone(snap)
        # Exactly one stale (pr, ruleset) pair: (pr, rs_two).
        self.assertEqual(snap.windows_stale, 1)

    def test_windows_stale_counts_missing_per_ruleset_state_pair(self) -> None:
        rs_two = QueueRuleSet.objects.create(
            repository=self.repo,
            version=3,
            require_open=True,
            require_not_draft=True,
            require_ci_success=False,
            is_active=True,
        )
        pr = self._mk_pr(12)
        PRRevision.objects.create(pull_request=pr, head_sha="z2", from_ts=pr.gh_created_at, to_ts=None, seq=0)
        built_at = timezone.now()
        PRRevisionBuildState.objects.create(
            pull_request=pr,
            revision_version=1,
            windows_built_revision_version=1,
            windows_built_at=built_at,
        )
        QueueRuleSet.objects.filter(pk=self.rule_set.pk).update(updated_at=built_at)
        QueueRuleSet.objects.filter(pk=rs_two.pk).update(updated_at=built_at)
        PRQueueWindowBuildState.objects.create(
            pull_request=pr,
            rule_set=self.rule_set,
            revision_version_built=1,
            windows_built_at=built_at,
            last_status="rebuilt",
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
        PRQueueWindow.objects.create(
            pull_request=pr,
            rule_set=rs_two,
            from_ts=pr.gh_created_at,
            to_ts=None,
            cycle_index=0,
            window_count=1,
            first_on_queue_ts=pr.gh_created_at,
        )

        collect_analyzer_convergence_task.apply().get()
        snap = AnalyzerConvergenceSnapshot.objects.filter(repository=self.repo).order_by("-collected_at").first()
        self.assertIsNotNone(snap)
        # Missing state row for rs_two counts as stale regardless of legacy PR-level fields.
        self.assertEqual(snap.windows_stale, 1)
