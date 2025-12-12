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
        # Two open PRs processed (the dependent and its target); closed PR counted but not processed.
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
        # Closed missing suppressed when only_open=True.
        self.assertEqual(res["categories"]["closed_missing"]["total"], 0)
        self.assertEqual(res["categories"]["closed_missing"]["queued"], 0)

    def test_builder_version_filter_skips_mismatched(self) -> None:
        pr_a = self._mk_pr(5, body="- [ ] depends on: #6")
        pr_b = self._mk_pr(6)
        # Seed a state with a different builder_version to ensure we can roll newer logic.
        PRDependencyState.objects.create(pull_request=pr_b, builder_version=2)

        res = rebuild_dependencies_sweep_task(max_prs_per_repo=10, only_open=True, builder_version=1)
        # pr_b processed as stale (builder_version mismatch); pr_a processed as missing.
        self.assertEqual(res["prs_processed"], 2)
        self.assertEqual(res["created"], 1)
        dep = PRDependency.objects.get(pull_request=pr_a, depends_on_number=6)
        self.assertEqual(dep.depends_on_pull_request_id, pr_b.id)
        state_a = PRDependencyState.objects.get(pull_request=pr_a)
        self.assertEqual(state_a.builder_version, 1)
        self.assertEqual(res["categories"]["open_missing"]["queued"], 1)
        self.assertEqual(res["categories"]["open_stale"]["queued"], 1)

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

    @patch("analyzer.tasks.dependencies.rebuild_pr_dependencies_task")
    def test_sweep_prioritizes_oldest_missing_states(self, mock_task) -> None:
        now = timezone.now()
        pr_old = self._mk_pr(9)
        pr_old.gh_updated_at = now - timezone.timedelta(days=30)
        pr_old.save(update_fields=["gh_updated_at"])
        pr_mid = self._mk_pr(10)
        pr_mid.gh_updated_at = now - timezone.timedelta(days=10)
        pr_mid.save(update_fields=["gh_updated_at"])
        pr_new = self._mk_pr(11)
        pr_new.gh_updated_at = now - timezone.timedelta(days=1)
        pr_new.save(update_fields=["gh_updated_at"])

        res = rebuild_dependencies_sweep_task(max_prs_per_repo=2, only_open=True, builder_version=1, fanout=True)

        self.assertEqual(res["categories"]["open_missing"]["queued"], 2)
        enqueued_ids = [call.args[0] for call in mock_task.delay.call_args_list]
        self.assertEqual(enqueued_ids, [pr_old.id, pr_mid.id])
        self.assertNotIn(pr_new.id, enqueued_ids)

    @patch("analyzer.tasks.dependencies.rebuild_pr_dependencies_task")
    def test_sweep_fills_remaining_with_least_recently_checked(self, mock_task) -> None:
        now = timezone.now()
        # Missing dependency state (should be processed first, oldest gh_updated first)
        pr_missing_old = self._mk_pr(12)
        pr_missing_old.gh_updated_at = now - timezone.timedelta(days=40)
        pr_missing_old.save(update_fields=["gh_updated_at"])
        pr_missing_new = self._mk_pr(13)
        pr_missing_new.gh_updated_at = now - timezone.timedelta(days=5)
        pr_missing_new.save(update_fields=["gh_updated_at"])

        # Existing dependency states with different last_checked_at
        pr_checked_old = self._mk_pr(14)
        PRDependencyState.objects.create(
            pull_request=pr_checked_old, last_checked_at=now - timezone.timedelta(days=20), builder_version=1
        )
        pr_checked_recent = self._mk_pr(15)
        PRDependencyState.objects.create(
            pull_request=pr_checked_recent, last_checked_at=now + timezone.timedelta(days=1), builder_version=1
        )

        res = rebuild_dependencies_sweep_task(max_prs_per_repo=3, only_open=True, builder_version=1, fanout=True)

        self.assertEqual(res["enqueued"], 3)
        enqueued_ids = [call.args[0] for call in mock_task.delay.call_args_list]
        # Missing-state PRs oldest-first, then stale open with earliest gh_updated_at.
        self.assertEqual(enqueued_ids, [pr_missing_old.id, pr_missing_new.id, pr_checked_old.id])
        self.assertNotIn(pr_checked_recent.id, enqueued_ids)
        self.assertEqual(res["categories"]["open_missing"]["queued"], 2)
        self.assertEqual(res["categories"]["open_stale"]["queued"], 1)

    @patch("analyzer.tasks.dependencies.rebuild_pr_dependencies_task")
    def test_sweep_prioritizes_open_missing_states_over_closed(self, mock_task) -> None:
        now = timezone.now()
        pr_closed_old = self._mk_pr(16, state="closed")
        pr_closed_old.gh_updated_at = now - timezone.timedelta(days=60)
        pr_closed_old.save(update_fields=["gh_updated_at"])
        pr_open_new = self._mk_pr(17, state="open")
        pr_open_new.gh_updated_at = now - timezone.timedelta(days=5)
        pr_open_new.save(update_fields=["gh_updated_at"])

        res = rebuild_dependencies_sweep_task(max_prs_per_repo=1, only_open=False, builder_version=1, fanout=True)

        enqueued_ids = [call.args[0] for call in mock_task.delay.call_args_list]
        self.assertEqual(enqueued_ids, [pr_open_new.id])
        self.assertNotIn(pr_closed_old.id, enqueued_ids)
        self.assertEqual(res["categories"]["open_missing"]["queued"], 1)
        self.assertEqual(res["categories"]["closed_missing"]["total"], 1)
        self.assertEqual(res["categories"]["closed_missing"]["queued"], 0)

    @patch("analyzer.tasks.dependencies.rebuild_pr_dependencies_task")
    def test_skips_when_already_checked_after_latest_update(self, mock_task) -> None:
        now = timezone.now()
        pr_missing = self._mk_pr(18)
        pr_missing.gh_updated_at = now - timezone.timedelta(days=15)
        pr_missing.save(update_fields=["gh_updated_at"])

        pr_checked = self._mk_pr(19)
        pr_checked.gh_updated_at = now - timezone.timedelta(days=20)
        pr_checked.save(update_fields=["gh_updated_at"])
        PRDependencyState.objects.create(
            pull_request=pr_checked, builder_version=1, last_checked_at=now - timezone.timedelta(days=5)
        )

        res = rebuild_dependencies_sweep_task(max_prs_per_repo=2, only_open=True, builder_version=1, fanout=True)

        enqueued_ids = [call.args[0] for call in mock_task.delay.call_args_list]
        # Checked after latest gh_updated_at -> skipped; missing-state still processed.
        self.assertEqual(enqueued_ids, [pr_missing.id])
        self.assertEqual(res["categories"]["open_stale"]["queued"], 0)

    @patch("analyzer.tasks.dependencies.rebuild_pr_dependencies_task")
    def test_closed_stale_is_queued_after_open_categories(self, mock_task) -> None:
        now = timezone.now()
        # Open missing
        pr_open_missing = self._mk_pr(20)
        pr_open_missing.gh_updated_at = now - timezone.timedelta(days=30)
        pr_open_missing.save(update_fields=["gh_updated_at"])
        # Open stale (checked long ago)
        pr_open_stale = self._mk_pr(21)
        PRDependencyState.objects.create(
            pull_request=pr_open_stale, builder_version=1, last_checked_at=now - timezone.timedelta(days=25)
        )
        # Closed stale (checked long ago)
        pr_closed_stale = self._mk_pr(22, state="closed")
        PRDependencyState.objects.create(
            pull_request=pr_closed_stale, builder_version=1, last_checked_at=now - timezone.timedelta(days=40)
        )

        res = rebuild_dependencies_sweep_task(max_prs_per_repo=3, only_open=False, builder_version=1, fanout=True)

        enqueued_ids = [call.args[0] for call in mock_task.delay.call_args_list]
        self.assertEqual(enqueued_ids, [pr_open_missing.id, pr_open_stale.id, pr_closed_stale.id])
        self.assertEqual(res["categories"]["open_missing"]["queued"], 1)
        self.assertEqual(res["categories"]["open_stale"]["queued"], 1)
        self.assertEqual(res["categories"]["closed_stale"]["queued"], 1)

    @patch("analyzer.tasks.dependencies.rebuild_pr_dependencies_task")
    def test_limit_respects_category_ordering(self, mock_task) -> None:
        now = timezone.now()
        pr_open_missing_a = self._mk_pr(23)
        pr_open_missing_a.gh_updated_at = now - timezone.timedelta(days=50)
        pr_open_missing_a.save(update_fields=["gh_updated_at"])
        pr_open_missing_b = self._mk_pr(24)
        pr_open_missing_b.gh_updated_at = now - timezone.timedelta(days=40)
        pr_open_missing_b.save(update_fields=["gh_updated_at"])

        pr_open_stale = self._mk_pr(25)
        PRDependencyState.objects.create(
            pull_request=pr_open_stale, builder_version=1, last_checked_at=now - timezone.timedelta(days=60)
        )

        pr_closed_missing = self._mk_pr(26, state="closed")
        pr_closed_missing.gh_updated_at = now - timezone.timedelta(days=70)
        pr_closed_missing.save(update_fields=["gh_updated_at"])

        res = rebuild_dependencies_sweep_task(max_prs_per_repo=2, only_open=False, builder_version=1, fanout=True)

        enqueued_ids = [call.args[0] for call in mock_task.delay.call_args_list]
        # Only open-missing fill the limit; others untouched.
        self.assertEqual(set(enqueued_ids), {pr_open_missing_a.id, pr_open_missing_b.id})
        self.assertEqual(res["categories"]["open_missing"]["queued"], 2)
        self.assertEqual(res["categories"]["open_stale"]["queued"], 0)
        self.assertEqual(res["categories"]["closed_missing"]["queued"], 0)
        self.assertEqual(res["categories"]["closed_stale"]["queued"], 0)

    @patch("analyzer.tasks.dependencies.rebuild_pr_dependencies_task")
    def test_stale_requires_updated_after_check(self, mock_task) -> None:
        now = timezone.now()
        pr_equal = self._mk_pr(27)
        pr_equal.gh_updated_at = now - timezone.timedelta(days=10)
        pr_equal.save(update_fields=["gh_updated_at"])
        PRDependencyState.objects.create(pull_request=pr_equal, builder_version=1, last_checked_at=pr_equal.gh_updated_at)

        pr_newer = self._mk_pr(28)
        pr_newer.gh_updated_at = now - timezone.timedelta(days=5)
        pr_newer.save(update_fields=["gh_updated_at"])
        PRDependencyState.objects.create(
            pull_request=pr_newer, builder_version=1, last_checked_at=now - timezone.timedelta(days=20)
        )

        res = rebuild_dependencies_sweep_task(max_prs_per_repo=2, only_open=True, builder_version=1, fanout=True)

        enqueued_ids = [call.args[0] for call in mock_task.delay.call_args_list]
        self.assertIn(pr_newer.id, enqueued_ids)
        self.assertNotIn(pr_equal.id, enqueued_ids)
        self.assertEqual(res["categories"]["open_stale"]["queued"], 1)
