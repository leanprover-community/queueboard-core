from __future__ import annotations

from django.test import TestCase
from django.utils import timezone

from analyzer.models import AnalyzerConvergenceSnapshot, PRRevision, PRRevisionBuildState, QueueRuleSet
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

        res = collect_analyzer_convergence_task.apply().get()
        snap = AnalyzerConvergenceSnapshot.objects.filter(repository=self.repo).order_by("-collected_at").first()
        self.assertIsNotNone(snap)
        self.assertEqual(snap.pr_no_revisions, 1)
        self.assertEqual(snap.windows_stale, 1)
        self.assertEqual(snap.ci_not_checked, 1)
        # CI-gated windows missing (pr1 no revisions, pr2 has revisions but no windows)
        self.assertGreaterEqual(snap.ci_gated_missing_windows, 1)
        self.assertEqual(snap.prs_missing_dependency_state, 2)
        self.assertEqual(snap.prs_stale_dependency_state, 0)
        self.assertEqual(res["rows_created"], 1)
