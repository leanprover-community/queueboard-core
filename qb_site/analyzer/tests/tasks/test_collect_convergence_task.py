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
from analyzer.models.queue_window import QueueWindowEventType
from analyzer.tasks.collect_convergence import collect_analyzer_convergence_task
from core.models import Repository
from syncer.models import CIShaFetchState, CommitCheckRun, PullRequest


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
        CommitCheckRun.objects.create(
            repository=self.repo,
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
        # Set gh_updated_at before built_at so it does not trigger gh_updated_at staleness.
        built_at = timezone.now()
        PullRequest.objects.filter(pk=pr.pk).update(gh_updated_at=built_at - timezone.timedelta(hours=1))
        pr.refresh_from_db()
        PRRevision.objects.create(pull_request=pr, head_sha="z1", from_ts=pr.gh_created_at, to_ts=None, seq=0)
        PRRevisionBuildState.objects.create(
            pull_request=pr,
            revision_version=1,
            windows_built_revision_version=1,
            windows_built_at=built_at,
        )
        QueueRuleSet.objects.filter(pk=self.rule_set.pk).update(updated_at=built_at - timezone.timedelta(hours=2))
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
            opened_by_event_type=QueueWindowEventType.INITIAL_STATE,
        )
        PRQueueWindow.objects.create(
            pull_request=pr,
            rule_set=rs_two,
            from_ts=pr.gh_created_at,
            to_ts=None,
            cycle_index=0,
            window_count=1,
            first_on_queue_ts=pr.gh_created_at,
            opened_by_event_type=QueueWindowEventType.INITIAL_STATE,
        )

        collect_analyzer_convergence_task.apply().get()
        snap = AnalyzerConvergenceSnapshot.objects.filter(repository=self.repo).order_by("-collected_at").first()
        self.assertIsNotNone(snap)
        # Exactly one stale (pr, ruleset) pair: (pr, rs_two) — rs_two.updated_at > built_at.
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
        # Set gh_updated_at before built_at so it does not trigger gh_updated_at staleness
        # on the existing (pr, rule_set) build-state row.
        built_at = timezone.now()
        PullRequest.objects.filter(pk=pr.pk).update(gh_updated_at=built_at - timezone.timedelta(hours=1))
        PRRevisionBuildState.objects.create(
            pull_request=pr,
            revision_version=1,
            windows_built_revision_version=1,
            windows_built_at=built_at,
        )
        QueueRuleSet.objects.filter(pk=self.rule_set.pk).update(updated_at=built_at - timezone.timedelta(hours=2))
        QueueRuleSet.objects.filter(pk=rs_two.pk).update(updated_at=built_at - timezone.timedelta(hours=2))
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
            opened_by_event_type=QueueWindowEventType.INITIAL_STATE,
        )
        PRQueueWindow.objects.create(
            pull_request=pr,
            rule_set=rs_two,
            from_ts=pr.gh_created_at,
            to_ts=None,
            cycle_index=0,
            window_count=1,
            first_on_queue_ts=pr.gh_created_at,
            opened_by_event_type=QueueWindowEventType.INITIAL_STATE,
        )

        collect_analyzer_convergence_task.apply().get()
        snap = AnalyzerConvergenceSnapshot.objects.filter(repository=self.repo).order_by("-collected_at").first()
        self.assertIsNotNone(snap)
        # Missing state row for rs_two counts as stale regardless of legacy PR-level fields.
        self.assertEqual(snap.windows_stale, 1)

    def test_windows_stale_detects_gh_updated_at_staleness(self) -> None:
        """windows_stale counts (PR, ruleset) pairs where windows were built before gh_updated_at.

        This covers label-only changes: GitHub bumps updated_at on label events, and
        windows built before that timestamp may no longer reflect current queue membership.
        """
        pr = self._mk_pr(20)
        PRRevision.objects.create(pull_request=pr, head_sha="s1", from_ts=pr.gh_created_at, to_ts=None, seq=0)
        # windows_built_at is before gh_updated_at (label event occurred after last rebuild).
        old_built_at = pr.gh_updated_at - timezone.timedelta(hours=2)
        PRRevisionBuildState.objects.create(pull_request=pr, revision_version=1)
        # Rule set updated_at is before windows_built_at — not the cause of staleness here.
        QueueRuleSet.objects.filter(pk=self.rule_set.pk).update(updated_at=old_built_at - timezone.timedelta(hours=1))
        self.rule_set.refresh_from_db()
        PRQueueWindowBuildState.objects.create(
            pull_request=pr,
            rule_set=self.rule_set,
            revision_version_built=1,
            windows_built_at=old_built_at,
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

        collect_analyzer_convergence_task.apply().get()
        snap = AnalyzerConvergenceSnapshot.objects.filter(repository=self.repo).order_by("-collected_at").first()
        self.assertIsNotNone(snap)
        self.assertEqual(snap.windows_stale, 1)

    def test_windows_stale_skips_when_windows_built_at_after_gh_updated_at(self) -> None:
        """windows_stale is 0 when windows were built after the last gh_updated_at."""
        pr = self._mk_pr(21)
        PRRevision.objects.create(pull_request=pr, head_sha="s2", from_ts=pr.gh_created_at, to_ts=None, seq=0)
        fresh_built_at = pr.gh_updated_at + timezone.timedelta(hours=1)
        PRRevisionBuildState.objects.create(pull_request=pr, revision_version=1)
        QueueRuleSet.objects.filter(pk=self.rule_set.pk).update(updated_at=pr.gh_updated_at - timezone.timedelta(hours=1))
        self.rule_set.refresh_from_db()
        PRQueueWindowBuildState.objects.create(
            pull_request=pr,
            rule_set=self.rule_set,
            revision_version_built=1,
            windows_built_at=fresh_built_at,
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
            opened_by_event_type=QueueWindowEventType.INITIAL_STATE,
        )

        collect_analyzer_convergence_task.apply().get()
        snap = AnalyzerConvergenceSnapshot.objects.filter(repository=self.repo).order_by("-collected_at").first()
        self.assertIsNotNone(snap)
        self.assertEqual(snap.windows_stale, 0)

    def test_ci_not_checked_uses_commit_scoped_ci_rows(self) -> None:
        pr_missing = self._mk_pr(60)
        PRRevision.objects.create(
            pull_request=pr_missing,
            head_sha="sha_missing",
            from_ts=pr_missing.gh_created_at,
            to_ts=None,
            seq=0,
        )

        pr_commit_ci = self._mk_pr(61)
        PRRevision.objects.create(
            pull_request=pr_commit_ci,
            head_sha="sha_commit",
            from_ts=pr_commit_ci.gh_created_at,
            to_ts=None,
            seq=0,
        )
        CommitCheckRun.objects.create(
            repository=self.repo,
            github_node_id="CCR61",
            head_sha="sha_commit",
            name="ci",
            status="COMPLETED",
            conclusion="SUCCESS",
            gh_started_at=timezone.now(),
            gh_completed_at=timezone.now(),
        )

        collect_analyzer_convergence_task.apply().get()
        snap = AnalyzerConvergenceSnapshot.objects.filter(repository=self.repo).order_by("-collected_at").first()
        self.assertIsNotNone(snap)
        self.assertEqual(snap.ci_not_checked, 1)

    def test_windows_unknown_attribution_counts_unknown_windows(self) -> None:
        """windows_unknown_attribution counts PRQueueWindow rows with UNKNOWN event type."""
        now = timezone.now()
        pr = self._mk_pr(70)

        # One window with UNKNOWN closed_by — should be counted.
        PRQueueWindow.objects.create(
            pull_request=pr,
            rule_set=self.rule_set,
            from_ts=now,
            to_ts=now + timezone.timedelta(hours=1),
            cycle_index=0,
            window_count=1,
            first_on_queue_ts=now,
            opened_by_event_type=QueueWindowEventType.CI_PASSED,
            closed_by_event_type=QueueWindowEventType.UNKNOWN,
        )
        # One window with UNKNOWN opened_by — should also be counted.
        PRQueueWindow.objects.create(
            pull_request=pr,
            rule_set=self.rule_set,
            from_ts=now + timezone.timedelta(hours=2),
            to_ts=None,
            cycle_index=1,
            window_count=1,
            first_on_queue_ts=now + timezone.timedelta(hours=2),
            opened_by_event_type=QueueWindowEventType.UNKNOWN,
        )
        # One window with clean attribution — must not be counted.
        pr2 = self._mk_pr(71)
        PRQueueWindow.objects.create(
            pull_request=pr2,
            rule_set=self.rule_set,
            from_ts=now,
            to_ts=None,
            cycle_index=0,
            window_count=1,
            first_on_queue_ts=now,
            opened_by_event_type=QueueWindowEventType.INITIAL_STATE,
        )

        collect_analyzer_convergence_task.apply().get()
        snap = AnalyzerConvergenceSnapshot.objects.filter(repository=self.repo).order_by("-collected_at").first()
        self.assertIsNotNone(snap)
        self.assertEqual(snap.windows_unknown_attribution, 2)

    def test_windows_unknown_attribution_zero_when_all_clean(self) -> None:
        """windows_unknown_attribution is 0 when no UNKNOWN windows exist."""
        now = timezone.now()
        pr = self._mk_pr(72)
        PRQueueWindow.objects.create(
            pull_request=pr,
            rule_set=self.rule_set,
            from_ts=now,
            to_ts=None,
            cycle_index=0,
            window_count=1,
            first_on_queue_ts=now,
            opened_by_event_type=QueueWindowEventType.CI_PASSED,
        )

        collect_analyzer_convergence_task.apply().get()
        snap = AnalyzerConvergenceSnapshot.objects.filter(repository=self.repo).order_by("-collected_at").first()
        self.assertIsNotNone(snap)
        self.assertEqual(snap.windows_unknown_attribution, 0)
