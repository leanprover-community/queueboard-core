"""Tests for W3: attribution fields persisted by rebuild_queue_windows_for_ruleset.

These tests verify the ten new columns on PRQueueWindow are written correctly,
overwritten on re-rebuild, and left null for open windows.
"""

from __future__ import annotations

from datetime import datetime, timezone as dt_timezone

from django.test import TestCase

from core.models import Repository
from analyzer.models import QueueRuleSet, PRQueueWindow, PRRevision
from analyzer.models.queue_window import QueueWindowEventType
from analyzer.services.queue_windows import rebuild_queue_windows_for_ruleset
from syncer.models import (
    CommitCheckRun,
    CommitStatusContext,
    PullRequest,
    PRTimelineEvent,
    PRTimelineEventType,
)


def _dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=dt_timezone.utc)


class _Base(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="o", name="r", default_branch="master", is_active=True)

    def _mk_pr(self, number: int, *, head_sha: str | None = None) -> PullRequest:
        pr = PullRequest.objects.create(
            repository=self.repo,
            number=number,
            author=None,
            state="open",
            is_draft=False,
            gh_created_at=_dt(2024, 9, 1),
            gh_updated_at=_dt(2024, 9, 2),
            base_ref_name="master",
            head_ref_name="branch",
            head_repo_owner_login="o",
            head_repo_name="r",
            title="t",
            body="",
            additions=0,
            deletions=0,
            changed_files_count=0,
            timeline_backfill_done=True,
        )
        if head_sha:
            pr.head_sha = head_sha
            pr.save(update_fields=["head_sha"])
        return pr

    def _add_revision(self, pr: PullRequest, sha: str, from_ts: datetime, to_ts: datetime | None, seq: int) -> PRRevision:
        return PRRevision.objects.create(pull_request=pr, head_sha=sha, from_ts=from_ts, to_ts=to_ts, seq=seq)

    def _label_ruleset(self, *, required: list[str] | None = None, forbidden: list[str] | None = None) -> QueueRuleSet:
        return QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            require_open=True,
            require_not_draft=True,
            require_ci_success=False,
            required_label_names=required or [],
            forbidden_label_names=forbidden or [],
        )

    def _ci_ruleset(self) -> QueueRuleSet:
        return QueueRuleSet.objects.create(
            repository=self.repo,
            version=2,
            require_open=True,
            require_not_draft=True,
            require_ci_success=True,
            required_label_names=[],
            forbidden_label_names=[],
            required_ci_contexts=["lint"],
        )

    def _mk_check_run(self, sha: str, *, node_id: str, conclusion: str, ts: datetime) -> CommitCheckRun:
        return CommitCheckRun.objects.create(
            repository=self.repo,
            github_node_id=node_id,
            head_sha=sha,
            name="lint",
            status="COMPLETED",
            conclusion=conclusion,
            details_url=None,
            external_id=None,
            gh_started_at=ts,
            gh_completed_at=ts,
        )


class TestLabelAttributionPersisted(_Base):
    def test_initial_state_open_window_persisted(self) -> None:
        """INITIAL_STATE open window: event_type set, all FKs null, closed_by_* null."""
        rule_set = self._label_ruleset()
        pr = self._mk_pr(1)

        rebuild_queue_windows_for_ruleset(pr=pr, rule_set=rule_set, as_of=_dt(2024, 9, 10))

        w = PRQueueWindow.objects.get(pull_request=pr, rule_set=rule_set)
        self.assertEqual(w.opened_by_event_type, QueueWindowEventType.INITIAL_STATE)
        self.assertIsNone(w.opened_by_timeline_event_id)
        self.assertIsNone(w.opened_by_check_run_id)
        self.assertIsNone(w.opened_by_status_context_id)
        self.assertIsNone(w.opened_at_head_sha)
        self.assertIsNone(w.closed_by_event_type)
        self.assertIsNone(w.closed_by_timeline_event_id)
        self.assertIsNone(w.closed_by_check_run_id)
        self.assertIsNone(w.closed_by_status_context_id)
        self.assertIsNone(w.closed_at_head_sha)

    def test_required_label_open_and_close_persisted(self) -> None:
        """Required label add/remove: correct event types and timeline_event FKs persisted."""
        rule_set = self._label_ruleset(required=["ready-to-merge"])
        pr = self._mk_pr(2)
        ev_add = PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.LABELED,
            occurred_at=_dt(2024, 9, 3),
            label_name="ready-to-merge",
        )
        ev_remove = PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.UNLABELED,
            occurred_at=_dt(2024, 9, 7),
            label_name="ready-to-merge",
        )

        rebuild_queue_windows_for_ruleset(pr=pr, rule_set=rule_set, as_of=_dt(2024, 9, 10))

        w = PRQueueWindow.objects.get(pull_request=pr, rule_set=rule_set)
        self.assertEqual(w.opened_by_event_type, QueueWindowEventType.REQUIRED_LABEL_ADDED)
        self.assertEqual(w.opened_by_timeline_event_id, ev_add.pk)
        self.assertEqual(w.closed_by_event_type, QueueWindowEventType.REQUIRED_LABEL_REMOVED)
        self.assertEqual(w.closed_by_timeline_event_id, ev_remove.pk)
        self.assertIsNone(w.opened_by_check_run_id)
        self.assertIsNone(w.opened_by_status_context_id)

    def test_forbidden_label_close_persisted(self) -> None:
        """Forbidden label added closes window: event type and FK persisted."""
        rule_set = self._label_ruleset(forbidden=["blocked"])
        pr = self._mk_pr(3)
        ev = PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.LABELED,
            occurred_at=_dt(2024, 9, 5),
            label_name="blocked",
        )

        rebuild_queue_windows_for_ruleset(pr=pr, rule_set=rule_set, as_of=_dt(2024, 9, 10))

        w = PRQueueWindow.objects.get(pull_request=pr, rule_set=rule_set)
        self.assertEqual(w.closed_by_event_type, QueueWindowEventType.FORBIDDEN_LABEL_ADDED)
        self.assertEqual(w.closed_by_timeline_event_id, ev.pk)

    def test_pr_closed_trailing_window_persisted(self) -> None:
        """PR_CLOSED attribution persisted for window closed by pr.closed_at."""
        rule_set = self._label_ruleset()
        pr = self._mk_pr(4)
        pr.closed_at = _dt(2024, 9, 7)
        pr.save(update_fields=["closed_at"])

        rebuild_queue_windows_for_ruleset(pr=pr, rule_set=rule_set, as_of=_dt(2024, 9, 10))

        w = PRQueueWindow.objects.get(pull_request=pr, rule_set=rule_set)
        self.assertEqual(w.closed_by_event_type, QueueWindowEventType.PR_CLOSED)
        self.assertIsNone(w.closed_by_timeline_event_id)


class TestCIAttributionPersisted(_Base):
    def test_ci_passed_check_run_fk_persisted(self) -> None:
        """CI_PASSED via check run: check_run FK and head SHA persisted."""
        rule_set = self._ci_ruleset()
        pr = self._mk_pr(10)
        self._add_revision(pr, "sha1", _dt(2024, 9, 1), None, 0)
        cr = self._mk_check_run("sha1", node_id="CR_OK", conclusion="SUCCESS", ts=_dt(2024, 9, 4))

        rebuild_queue_windows_for_ruleset(pr=pr, rule_set=rule_set, as_of=_dt(2024, 9, 10))

        w = PRQueueWindow.objects.get(pull_request=pr, rule_set=rule_set)
        self.assertEqual(w.opened_by_event_type, QueueWindowEventType.CI_PASSED)
        self.assertEqual(w.opened_by_check_run_id, cr.pk)
        self.assertIsNone(w.opened_by_timeline_event_id)
        self.assertIsNone(w.opened_by_status_context_id)
        self.assertEqual(w.opened_at_head_sha, "sha1")
        self.assertIsNone(w.closed_by_event_type)

    def test_ci_failed_check_run_fk_persisted(self) -> None:
        """CI_FAILED via check run: check_run FK persisted on closed_by."""
        rule_set = self._ci_ruleset()
        pr = self._mk_pr(11)
        self._add_revision(pr, "sha1", _dt(2024, 9, 1), None, 0)
        self._mk_check_run("sha1", node_id="CR_OK", conclusion="SUCCESS", ts=_dt(2024, 9, 3))
        cr_fail = self._mk_check_run("sha1", node_id="CR_FAIL", conclusion="FAILURE", ts=_dt(2024, 9, 7))

        rebuild_queue_windows_for_ruleset(pr=pr, rule_set=rule_set, as_of=_dt(2024, 9, 10))

        w = PRQueueWindow.objects.get(pull_request=pr, rule_set=rule_set)
        self.assertEqual(w.closed_by_event_type, QueueWindowEventType.CI_FAILED)
        self.assertEqual(w.closed_by_check_run_id, cr_fail.pk)
        self.assertIsNone(w.closed_by_timeline_event_id)
        self.assertIsNone(w.closed_by_status_context_id)

    def test_ci_passed_status_context_fk_persisted(self) -> None:
        """CI_PASSED via status context: status_context FK persisted."""
        rule_set = self._ci_ruleset()
        pr = self._mk_pr(12)
        self._add_revision(pr, "sha1", _dt(2024, 9, 1), None, 0)
        sc = CommitStatusContext.objects.create(
            repository=self.repo,
            github_node_id="SC_OK",
            head_sha="sha1",
            name="lint",
            state="SUCCESS",
            description="",
            target_url=None,
            gh_created_at=_dt(2024, 9, 4),
        )

        rebuild_queue_windows_for_ruleset(pr=pr, rule_set=rule_set, as_of=_dt(2024, 9, 10))

        w = PRQueueWindow.objects.get(pull_request=pr, rule_set=rule_set)
        self.assertEqual(w.opened_by_event_type, QueueWindowEventType.CI_PASSED)
        self.assertEqual(w.opened_by_status_context_id, sc.pk)
        self.assertIsNone(w.opened_by_check_run_id)

    def test_head_pushed_attribution_persisted(self) -> None:
        """HEAD_PUSHED attribution persisted at revision boundary."""
        rule_set = self._ci_ruleset()
        pr = self._mk_pr(13)
        self._add_revision(pr, "sha1", _dt(2024, 9, 1), _dt(2024, 9, 6), 0)
        self._add_revision(pr, "sha2", _dt(2024, 9, 6), None, 1)
        self._mk_check_run("sha1", node_id="CR_sha1_OK", conclusion="SUCCESS", ts=_dt(2024, 9, 3))

        rebuild_queue_windows_for_ruleset(pr=pr, rule_set=rule_set, as_of=_dt(2024, 9, 10))

        w = PRQueueWindow.objects.get(pull_request=pr, rule_set=rule_set)
        self.assertEqual(w.closed_by_event_type, QueueWindowEventType.HEAD_PUSHED)
        self.assertIsNone(w.closed_by_timeline_event_id)
        self.assertIsNone(w.closed_by_check_run_id)
        self.assertIsNone(w.closed_by_status_context_id)


class TestAttributionOverwrittenOnRebuild(_Base):
    def test_attribution_overwritten_on_re_rebuild(self) -> None:
        """Re-running rebuild overwrites attribution fields, not just rollup fields."""
        rule_set = self._label_ruleset(required=["ready-to-merge"])
        pr = self._mk_pr(20)
        ev1 = PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.LABELED,
            occurred_at=_dt(2024, 9, 3),
            label_name="ready-to-merge",
        )

        # First build: window opens via ev1.
        rebuild_queue_windows_for_ruleset(pr=pr, rule_set=rule_set, as_of=_dt(2024, 9, 10))
        w = PRQueueWindow.objects.get(pull_request=pr, rule_set=rule_set)
        self.assertEqual(w.opened_by_timeline_event_id, ev1.pk)

        # Add a close event and rebuild.
        ev2 = PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.UNLABELED,
            occurred_at=_dt(2024, 9, 8),
            label_name="ready-to-merge",
        )
        rebuild_queue_windows_for_ruleset(pr=pr, rule_set=rule_set, as_of=_dt(2024, 9, 10))

        w.refresh_from_db()
        # opened_by FK unchanged; closed_by FK now set.
        self.assertEqual(w.opened_by_timeline_event_id, ev1.pk)
        self.assertEqual(w.closed_by_event_type, QueueWindowEventType.REQUIRED_LABEL_REMOVED)
        self.assertEqual(w.closed_by_timeline_event_id, ev2.pk)

    def test_null_attribution_pre_migration_overwritten(self) -> None:
        """Existing windows with null attribution (pre-migration) are overwritten on rebuild."""
        rule_set = self._label_ruleset()
        pr = self._mk_pr(21)

        # Simulate a pre-migration row: all attribution fields null.
        PRQueueWindow.objects.create(
            pull_request=pr,
            rule_set=rule_set,
            from_ts=_dt(2024, 9, 1),
            to_ts=None,
            cycle_index=0,
            duration_seconds_closed=0,
            cumulative_seconds_closed=0,
            window_count=1,
            first_on_queue_ts=_dt(2024, 9, 1),
        )

        rebuild_queue_windows_for_ruleset(pr=pr, rule_set=rule_set, as_of=_dt(2024, 9, 10))

        w = PRQueueWindow.objects.get(pull_request=pr, rule_set=rule_set)
        # After rebuild, INITIAL_STATE should be populated.
        self.assertEqual(w.opened_by_event_type, QueueWindowEventType.INITIAL_STATE)
