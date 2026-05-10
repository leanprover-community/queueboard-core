"""Tests for W4b: sweep staleness extension for attribution backfill.

Verifies that the rebuild_queue_windows_sweep_task detects and rebuilds:
- Pre-migration windows with null opened_by_event_type.
- Windows with CI event_type but both CI FKs null (post-expire-task partial failure).
"""

from __future__ import annotations

from datetime import datetime, timezone as dt_timezone

from django.test import TestCase
from django.utils import timezone

from analyzer.models import PRQueueWindow, PRQueueWindowBuildState, PRRevision, PRRevisionBuildState, QueueRuleSet
from analyzer.models.queue_window import QueueWindowEventType
from analyzer.tasks.rebuild_queue_windows_sweep import rebuild_queue_windows_sweep_task
from core.models import Repository
from syncer.models import PullRequest


def _dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=dt_timezone.utc)


class _Base(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="o", name="r", default_branch="master", is_active=True)
        self.rule_set = QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            require_open=True,
            require_not_draft=True,
            require_ci_success=False,
            required_label_names=[],
            forbidden_label_names=[],
        )

    def _mk_pr(self, number: int) -> PullRequest:
        now = timezone.now()
        return PullRequest.objects.create(
            repository=self.repo,
            number=number,
            author=None,
            state="open",
            is_draft=False,
            gh_created_at=now - timezone.timedelta(days=2),
            gh_updated_at=now - timezone.timedelta(hours=2),
            base_ref_name="master",
            head_ref_name="branch",
            head_repo_owner_login="o",
            head_repo_name="fork",
            title="t",
            body="",
            additions=0,
            deletions=0,
            changed_files_count=0,
            timeline_backfill_done=True,
            commits_backfill_done=True,
        )

    def _seed_up_to_date_build_state(self, pr: PullRequest, *, version: int = 1) -> PRQueueWindowBuildState:
        """Seed a fully up-to-date revision + build state so the sweep only fires for our target condition."""
        built_at = timezone.now() - timezone.timedelta(minutes=5)
        PRRevisionBuildState.objects.create(
            pull_request=pr,
            revision_version=version,
        )
        PRRevision.objects.create(pull_request=pr, head_sha="sha1", from_ts=pr.gh_created_at, to_ts=None, seq=0)
        return PRQueueWindowBuildState.objects.create(
            pull_request=pr,
            rule_set=self.rule_set,
            revision_version_built=version,
            windows_built_at=built_at,
            last_status="rebuilt",
        )


class TestSweepAttributionBackfill(_Base):
    def test_pre_migration_null_event_type_triggers_rebuild(self) -> None:
        """Window with null opened_by_event_type is detected as stale and rebuilt."""
        pr = self._mk_pr(1)
        self._seed_up_to_date_build_state(pr)
        # Simulate a pre-migration window: attribution fields all null.
        PRQueueWindow.objects.create(
            pull_request=pr,
            rule_set=self.rule_set,
            from_ts=pr.gh_created_at,
            to_ts=None,
            cycle_index=0,
            window_count=1,
            first_on_queue_ts=pr.gh_created_at,
            opened_by_event_type=None,  # pre-migration: not yet set
        )

        res = rebuild_queue_windows_sweep_task.apply(kwargs={"max_prs_per_repo": 5}).get()

        self.assertEqual(res["windows_rebuilt"], 1)
        win = PRQueueWindow.objects.get(pull_request=pr, rule_set=self.rule_set)
        # After rebuild, INITIAL_STATE should be populated.
        self.assertEqual(win.opened_by_event_type, QueueWindowEventType.INITIAL_STATE)

    def test_ci_event_type_with_null_fks_triggers_rebuild(self) -> None:
        """Window with CI_PASSED event_type but both CI FKs null is detected and rebuilt."""
        pr = self._mk_pr(2)
        self._seed_up_to_date_build_state(pr)
        # Simulate post-expire-task partial failure: FK was nulled, event_type still set.
        PRQueueWindow.objects.create(
            pull_request=pr,
            rule_set=self.rule_set,
            from_ts=pr.gh_created_at,
            to_ts=None,
            cycle_index=0,
            window_count=1,
            first_on_queue_ts=pr.gh_created_at,
            opened_by_event_type=QueueWindowEventType.CI_PASSED,
            opened_by_check_run=None,
            opened_by_status_context=None,
        )

        res = rebuild_queue_windows_sweep_task.apply(kwargs={"max_prs_per_repo": 5}).get()

        # Rebuild fires; since no CI rows exist for this label-only ruleset, the window
        # is rebuilt from scratch with INITIAL_STATE.
        self.assertEqual(res["windows_rebuilt"], 1)
        win = PRQueueWindow.objects.get(pull_request=pr, rule_set=self.rule_set)
        self.assertNotEqual(win.opened_by_event_type, QueueWindowEventType.CI_PASSED)

    def test_window_with_correct_attribution_not_spuriously_rebuilt(self) -> None:
        """Window with proper INITIAL_STATE attribution is not detected as stale."""
        pr = self._mk_pr(3)
        self._seed_up_to_date_build_state(pr)
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

        res = rebuild_queue_windows_sweep_task.apply(kwargs={"max_prs_per_repo": 5}).get()

        self.assertEqual(res["windows_rebuilt"], 0)

    def test_closed_by_ci_event_type_with_null_fks_triggers_rebuild(self) -> None:
        """Window with CI_FAILED closed_by_event_type but both closed CI FKs null is detected."""
        pr = self._mk_pr(4)
        self._seed_up_to_date_build_state(pr)
        PRQueueWindow.objects.create(
            pull_request=pr,
            rule_set=self.rule_set,
            from_ts=pr.gh_created_at,
            to_ts=pr.gh_created_at + timezone.timedelta(days=1),
            cycle_index=0,
            window_count=1,
            first_on_queue_ts=pr.gh_created_at,
            opened_by_event_type=QueueWindowEventType.INITIAL_STATE,
            closed_by_event_type=QueueWindowEventType.CI_FAILED,
            closed_by_check_run=None,
            closed_by_status_context=None,
        )

        res = rebuild_queue_windows_sweep_task.apply(kwargs={"max_prs_per_repo": 5}).get()

        self.assertEqual(res["windows_rebuilt"], 1)
