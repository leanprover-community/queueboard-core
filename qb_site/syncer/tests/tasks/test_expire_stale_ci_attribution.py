"""Tests for W4a: expire task marks PRQueueWindow build states stale and enqueues rebuilds.

Verifies that before each CI deletion pass, any PRQueueWindow rows whose attribution
FKs reference the about-to-be-deleted rows cause the owning PRs to be marked stale
(windows_built_at=None on PRQueueWindowBuildState) and enqueued for rebuild.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from analyzer.models import PRQueueWindow, PRQueueWindowBuildState, QueueRuleSet
from analyzer.models.queue_window import QueueWindowEventType
from syncer.models import CommitCheckRun, CommitStatusContext
from syncer.tasks.sync_tasks import expire_stale_ci_for_repo_task
from syncer.tests.factories import make_repo, make_pr


def _dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=dt_timezone.utc)


class _Base(TestCase):
    def setUp(self) -> None:
        self.repo = make_repo()
        self.pr = make_pr(self.repo, 1)
        self.rule_set = QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            require_open=True,
            require_not_draft=True,
            require_ci_success=True,
            required_label_names=[],
            forbidden_label_names=[],
            required_ci_contexts=["lint"],
        )
        self.build_state = PRQueueWindowBuildState.objects.create(
            pull_request=self.pr,
            rule_set=self.rule_set,
            revision_version_built=1,
            windows_built_at=_dt(2024, 9, 5),
        )

    def _mk_check_run(self, *, node_id: str, sha: str = "sha1", conclusion: str = "SUCCESS") -> CommitCheckRun:
        return CommitCheckRun.objects.create(
            repository=self.repo,
            github_node_id=node_id,
            head_sha=sha,
            name="lint",
            status="COMPLETED",
            conclusion=conclusion,
            gh_started_at=_dt(2024, 9, 3),
            gh_completed_at=_dt(2024, 9, 3),
        )

    def _mk_status_context(self, *, node_id: str, sha: str = "sha1", state: str = "SUCCESS") -> CommitStatusContext:
        return CommitStatusContext.objects.create(
            repository=self.repo,
            github_node_id=node_id,
            head_sha=sha,
            name="lint",
            state=state,
            description="",
            target_url=None,
            gh_created_at=_dt(2024, 9, 3),
        )

    def _mk_window(
        self,
        *,
        opened_by_check_run: CommitCheckRun | None = None,
        closed_by_check_run: CommitCheckRun | None = None,
        opened_by_status_context: CommitStatusContext | None = None,
        closed_by_status_context: CommitStatusContext | None = None,
    ) -> PRQueueWindow:
        return PRQueueWindow.objects.create(
            pull_request=self.pr,
            rule_set=self.rule_set,
            from_ts=_dt(2024, 9, 3),
            to_ts=_dt(2024, 9, 8) if (closed_by_check_run or closed_by_status_context) else None,
            cycle_index=0,
            duration_seconds_closed=0,
            cumulative_seconds_closed=0,
            window_count=1,
            first_on_queue_ts=_dt(2024, 9, 3),
            opened_by_event_type=QueueWindowEventType.CI_PASSED,
            opened_by_check_run=opened_by_check_run,
            opened_by_status_context=opened_by_status_context,
            opened_at_head_sha="sha1",
            closed_by_event_type=QueueWindowEventType.CI_FAILED if (closed_by_check_run or closed_by_status_context) else None,
            closed_by_check_run=closed_by_check_run,
            closed_by_status_context=closed_by_status_context,
        )


class TestExpireTaskInvalidatesWindowsOnCheckRunDeletion(_Base):
    def test_superseded_check_run_marks_build_state_stale(self) -> None:
        """Superseded check run referenced by opened_by_check_run causes build state to be nulled."""
        cr_old = self._mk_check_run(node_id="CR_OLD")
        self._mk_check_run(node_id="CR_NEW")  # newer supersedes old
        self._mk_window(opened_by_check_run=cr_old)

        with patch("analyzer.tasks.process_pr_task") as mock_task:
            expire_stale_ci_for_repo_task(self.repo.id, stale_pending_days=0)

        self.build_state.refresh_from_db()
        self.assertIsNone(self.build_state.windows_built_at)
        mock_task.delay.assert_called_once_with(self.pr.id)

    def test_superseded_check_run_in_closed_by_marks_stale(self) -> None:
        """Superseded check run referenced by closed_by_check_run also triggers invalidation."""
        cr_ok = self._mk_check_run(node_id="CR_OK")
        cr_fail_old = self._mk_check_run(node_id="CR_FAIL_OLD", conclusion="FAILURE")
        self._mk_check_run(node_id="CR_FAIL_NEW", conclusion="FAILURE")  # newer
        self._mk_window(opened_by_check_run=cr_ok, closed_by_check_run=cr_fail_old)

        with patch("analyzer.tasks.process_pr_task") as mock_task:
            expire_stale_ci_for_repo_task(self.repo.id, stale_pending_days=0)

        self.build_state.refresh_from_db()
        self.assertIsNone(self.build_state.windows_built_at)
        mock_task.delay.assert_called_once_with(self.pr.id)

    def test_stale_pending_check_run_marks_stale(self) -> None:
        """Stale pending check run referenced by a window attribution triggers invalidation (pass 1)."""
        cr_pending = CommitCheckRun.objects.create(
            repository=self.repo,
            github_node_id="CR_PENDING",
            head_sha="sha1",
            name="lint",
            status="IN_PROGRESS",
            conclusion=None,
            gh_started_at=timezone.now() - timedelta(days=40),
            gh_completed_at=None,
        )
        self._mk_window(opened_by_check_run=cr_pending)

        with patch("analyzer.tasks.process_pr_task") as mock_task:
            expire_stale_ci_for_repo_task(self.repo.id, stale_pending_days=30)

        self.build_state.refresh_from_db()
        self.assertIsNone(self.build_state.windows_built_at)
        mock_task.delay.assert_called()

    def test_unrelated_check_run_deletion_does_not_mark_stale(self) -> None:
        """Deleting a check run not referenced by any window does not affect build state."""
        self._mk_check_run(node_id="CR_OLD")
        self._mk_check_run(node_id="CR_NEW")
        # Window references no check run (label-only attribution).
        PRQueueWindow.objects.create(
            pull_request=self.pr,
            rule_set=self.rule_set,
            from_ts=_dt(2024, 9, 3),
            to_ts=None,
            cycle_index=0,
            duration_seconds_closed=0,
            cumulative_seconds_closed=0,
            window_count=1,
            first_on_queue_ts=_dt(2024, 9, 3),
            opened_by_event_type=QueueWindowEventType.REQUIRED_LABEL_ADDED,
            opened_at_head_sha="sha1",
        )

        with patch("analyzer.tasks.process_pr_task") as mock_task:
            expire_stale_ci_for_repo_task(self.repo.id, stale_pending_days=0)

        self.build_state.refresh_from_db()
        # Build state still has windows_built_at set — not invalidated.
        self.assertIsNotNone(self.build_state.windows_built_at)
        mock_task.delay.assert_not_called()

    def test_result_includes_prs_invalidated_counts(self) -> None:
        """Return dict includes prs_invalidated_* counts for observability."""
        cr_old = self._mk_check_run(node_id="CR_OLD")
        self._mk_check_run(node_id="CR_NEW")
        self._mk_window(opened_by_check_run=cr_old)

        with patch("analyzer.tasks.process_pr_task"):
            result = expire_stale_ci_for_repo_task(self.repo.id, stale_pending_days=0)

        self.assertEqual(result["prs_invalidated_superseded_check_runs"], 1)
        self.assertIn("prs_invalidated_stale_pending_check_runs", result)
        self.assertIn("prs_invalidated_superseded_status_contexts", result)


class TestExpireTaskInvalidatesWindowsOnStatusContextDeletion(_Base):
    def test_superseded_status_context_marks_build_state_stale(self) -> None:
        """Superseded status context referenced by opened_by_status_context triggers invalidation."""
        sc_old = self._mk_status_context(node_id="SC_OLD")
        self._mk_status_context(node_id="SC_NEW")  # newer supersedes old
        self._mk_window(opened_by_status_context=sc_old)

        with patch("analyzer.tasks.process_pr_task") as mock_task:
            expire_stale_ci_for_repo_task(self.repo.id, stale_pending_days=0)

        self.build_state.refresh_from_db()
        self.assertIsNone(self.build_state.windows_built_at)
        mock_task.delay.assert_called_once_with(self.pr.id)
