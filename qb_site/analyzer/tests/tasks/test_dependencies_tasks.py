from __future__ import annotations

from django.test import TestCase
from django.utils import timezone

from analyzer.models import PRDependency
from analyzer.models import PRDependencyState
from analyzer.tasks.dependencies import rebuild_pr_dependencies_task, rebuild_dependencies_sweep_task
from core.models import Repository
from syncer.models import PullRequest
from unittest.mock import patch


class DependencyTaskTests(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="o", name="r", default_branch="main", is_active=True)

    def _mk_pr(self, number: int, *, body: str = "", state: str = "open") -> PullRequest:
        now = timezone.now()
        return PullRequest.objects.create(
            repository=self.repo,
            number=number,
            author=None,
            state=state,
            is_draft=False,
            gh_created_at=now - timezone.timedelta(days=1),
            gh_updated_at=now - timezone.timedelta(hours=1),
            base_ref_name="main",
            head_ref_name=f"branch-{number}",
            head_repo_owner_login=self.repo.owner,
            head_repo_name=self.repo.name,
            title=f"PR {number}",
            body=body,
            additions=1,
            deletions=0,
            changed_files_count=0,
        )

    def test_rebuild_pr_dependencies_task_builds_edges(self) -> None:
        pr1 = self._mk_pr(1, body="- [ ] depends on: #2")
        pr2 = self._mk_pr(2)
        res = rebuild_pr_dependencies_task(pr1.id)
        self.assertFalse(res["skipped"])
        self.assertEqual(res["created"], 1)
        self.assertEqual(res["resolved_numbers"], [2])
        dep = PRDependency.objects.get(pull_request=pr1, depends_on_number=2)
        self.assertEqual(dep.depends_on_pull_request_id, pr2.id)
        state = PRDependencyState.objects.get(pull_request=pr1)
        self.assertEqual(state.builder_version, 1)

    def test_rebuild_dependencies_sweep_task_skips_closed_when_requested(self) -> None:
        pr_open = self._mk_pr(3, body="- [ ] depends on: #2")
        pr_target = self._mk_pr(2)
        pr_closed = self._mk_pr(4, body="- [ ] depends on: #2", state="closed")
        res = rebuild_dependencies_sweep_task(max_prs_per_repo=5, only_open=True)
        # Two open PRs processed (the dependent and its target); closed PR skipped.
        self.assertEqual(res["prs_processed"], 2)
        self.assertEqual(res["created"], 1)
        self.assertEqual(
            list(PRDependency.objects.filter(pull_request=pr_open).values_list("depends_on_pull_request", flat=True)),
            [pr_target.id],
        )
        self.assertFalse(PRDependency.objects.filter(pull_request=pr_closed).exists())

        state = PRDependencyState.objects.get(pull_request=pr_open)
        self.assertIsNotNone(state.last_checked_at)
        self.assertEqual(state.builder_version, 1)

    def test_builder_version_filter_skips_mismatched(self) -> None:
        pr_a = self._mk_pr(5, body="- [ ] depends on: #6")
        pr_b = self._mk_pr(6)
        # Seed a state with a different builder_version to ensure we can roll newer logic.
        PRDependencyState.objects.create(pull_request=pr_b, builder_version=2)

        res = rebuild_dependencies_sweep_task(max_prs_per_repo=10, only_open=True, builder_version=1)
        # pr_b is skipped due to builder_version mismatch; pr_a processed.
        self.assertEqual(res["prs_processed"], 1)
        self.assertEqual(res["created"], 1)
        dep = PRDependency.objects.get(pull_request=pr_a, depends_on_number=6)
        self.assertEqual(dep.depends_on_pull_request_id, pr_b.id)
        state_a = PRDependencyState.objects.get(pull_request=pr_a)
        self.assertEqual(state_a.builder_version, 1)

    @patch("analyzer.tasks.dependencies.rebuild_pr_dependencies_task")
    def test_fanout_mode_enqueues_tasks(self, mock_task) -> None:
        mock_task.delay.return_value = type("Res", (), {"id": "task123"})
        pr1 = self._mk_pr(7, body="- [ ] depends on: #8")
        self._mk_pr(8)
        res = rebuild_dependencies_sweep_task(max_prs_per_repo=10, only_open=True, builder_version=1, fanout=True)
        self.assertEqual(res["enqueued"], 2)  # both the dependent and target are enqueued
        mock_task.delay.assert_any_call(pr1.id, builder_version=1)
        # No dependency rows should be created inline in fanout mode.
        self.assertEqual(PRDependency.objects.count(), 0)
