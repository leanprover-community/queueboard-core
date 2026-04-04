"""Tests for queue-window event attribution (W2: _queue_windows_with_rules).

These tests exercise _queue_windows_with_rules directly via the internal
helper so they can inspect WindowAttribution fields without going through
the DB persistence layer (which is covered in W3 tests).
"""

from __future__ import annotations

from datetime import datetime, timezone as dt_timezone

from django.test import TestCase

from core.models import Repository
from analyzer.models import QueueRuleSet, PRRevision
from analyzer.models.queue_window import QueueWindowEventType
from analyzer.services.queue_rules import rules_for_rule_set
from analyzer.services.queue_windows import _queue_windows_with_rules
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
    """Shared fixtures for attribution tests."""

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

    def _ci_ruleset(self, *, contexts: list[str] | None = None) -> QueueRuleSet:
        return QueueRuleSet.objects.create(
            repository=self.repo,
            version=2,
            require_open=True,
            require_not_draft=True,
            require_ci_success=True,
            required_label_names=[],
            forbidden_label_names=[],
            required_ci_contexts=contexts or ["lint"],
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

    def _mk_status_context(self, sha: str, *, node_id: str, state: str, ts: datetime) -> CommitStatusContext:
        return CommitStatusContext.objects.create(
            repository=self.repo,
            github_node_id=node_id,
            head_sha=sha,
            name="lint",
            state=state,
            description="",
            target_url=None,
            gh_created_at=ts,
        )


# ---------------------------------------------------------------------------
# Label-only path
# ---------------------------------------------------------------------------


class TestLabelOnlyAttribution(_Base):
    def test_initial_state_open(self) -> None:
        """PR eligible from creation → INITIAL_STATE open, no close attribution."""
        rule_set = self._label_ruleset()
        pr = self._mk_pr(1)
        rules = rules_for_rule_set(rule_set)

        windows = _queue_windows_with_rules(pr, rules=rules, as_of=_dt(2024, 9, 10))

        self.assertEqual(len(windows), 1)
        w = windows[0]
        self.assertEqual(w.opened_by.event_type, QueueWindowEventType.INITIAL_STATE)
        self.assertIsNone(w.opened_by.timeline_event)
        self.assertIsNone(w.closed_by)

    def test_initial_state_head_sha_is_none_without_revisions(self) -> None:
        """INITIAL_STATE open has null head_sha when no revisions exist."""
        rule_set = self._label_ruleset()
        pr = self._mk_pr(2)
        rules = rules_for_rule_set(rule_set)

        windows = _queue_windows_with_rules(pr, rules=rules, as_of=_dt(2024, 9, 10))

        self.assertEqual(len(windows), 1)
        self.assertIsNone(windows[0].opened_by.head_sha)

    def test_required_label_added_opens_window(self) -> None:
        """Window opens when a required label is added."""
        rule_set = self._label_ruleset(required=["ready-to-merge"])
        pr = self._mk_pr(3)
        ev = PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.LABELED,
            occurred_at=_dt(2024, 9, 5),
            label_name="ready-to-merge",
        )
        rules = rules_for_rule_set(rule_set)

        windows = _queue_windows_with_rules(pr, rules=rules, as_of=_dt(2024, 9, 10))

        self.assertEqual(len(windows), 1)
        w = windows[0]
        self.assertEqual(w.opened_by.event_type, QueueWindowEventType.REQUIRED_LABEL_ADDED)
        self.assertEqual(w.opened_by.timeline_event, ev)
        self.assertIsNone(w.closed_by)

    def test_required_label_removed_closes_window(self) -> None:
        """Window closes when a required label is removed."""
        rule_set = self._label_ruleset(required=["ready-to-merge"])
        pr = self._mk_pr(4)
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
        rules = rules_for_rule_set(rule_set)

        windows = _queue_windows_with_rules(pr, rules=rules, as_of=_dt(2024, 9, 10))

        self.assertEqual(len(windows), 1)
        w = windows[0]
        self.assertEqual(w.opened_by.event_type, QueueWindowEventType.REQUIRED_LABEL_ADDED)
        self.assertEqual(w.opened_by.timeline_event, ev_add)
        self.assertEqual(w.closed_by.event_type, QueueWindowEventType.REQUIRED_LABEL_REMOVED)
        self.assertEqual(w.closed_by.timeline_event, ev_remove)

    def test_forbidden_label_added_closes_window(self) -> None:
        """Window closes when a forbidden label is added."""
        rule_set = self._label_ruleset(forbidden=["blocked"])
        pr = self._mk_pr(5)
        ev = PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.LABELED,
            occurred_at=_dt(2024, 9, 5),
            label_name="blocked",
        )
        rules = rules_for_rule_set(rule_set)

        windows = _queue_windows_with_rules(pr, rules=rules, as_of=_dt(2024, 9, 10))

        self.assertEqual(len(windows), 1)
        w = windows[0]
        self.assertEqual(w.from_ts, _dt(2024, 9, 1))
        self.assertEqual(w.to_ts, _dt(2024, 9, 5))
        self.assertEqual(w.closed_by.event_type, QueueWindowEventType.FORBIDDEN_LABEL_ADDED)
        self.assertEqual(w.closed_by.timeline_event, ev)

    def test_forbidden_label_removed_opens_window(self) -> None:
        """Window opens when a forbidden label is removed."""
        rule_set = self._label_ruleset(forbidden=["blocked"])
        pr = self._mk_pr(6)
        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.LABELED,
            occurred_at=_dt(2024, 9, 1),
            label_name="blocked",
        )
        ev_remove = PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.UNLABELED,
            occurred_at=_dt(2024, 9, 5),
            label_name="blocked",
        )
        rules = rules_for_rule_set(rule_set)

        windows = _queue_windows_with_rules(pr, rules=rules, as_of=_dt(2024, 9, 10))

        self.assertEqual(len(windows), 1)
        w = windows[0]
        self.assertEqual(w.opened_by.event_type, QueueWindowEventType.FORBIDDEN_LABEL_REMOVED)
        self.assertEqual(w.opened_by.timeline_event, ev_remove)

    def test_draft_converted_opens_window(self) -> None:
        """Window opens when PR is marked ready for review."""
        rule_set = self._label_ruleset()
        pr = self._mk_pr(7)
        # First event is READY_FOR_REVIEW → PR was created as draft.
        ev = PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.READY_FOR_REVIEW,
            occurred_at=_dt(2024, 9, 5),
        )
        rules = rules_for_rule_set(rule_set)

        windows = _queue_windows_with_rules(pr, rules=rules, as_of=_dt(2024, 9, 10))

        self.assertEqual(len(windows), 1)
        w = windows[0]
        self.assertEqual(w.opened_by.event_type, QueueWindowEventType.DRAFT_CONVERTED)
        self.assertEqual(w.opened_by.timeline_event, ev)

    def test_converted_to_draft_closes_window(self) -> None:
        """Window closes when PR is converted to draft."""
        rule_set = self._label_ruleset()
        pr = self._mk_pr(8)
        ev = PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.CONVERT_TO_DRAFT,
            occurred_at=_dt(2024, 9, 5),
        )
        rules = rules_for_rule_set(rule_set)

        windows = _queue_windows_with_rules(pr, rules=rules, as_of=_dt(2024, 9, 10))

        self.assertEqual(len(windows), 1)
        w = windows[0]
        self.assertEqual(w.closed_by.event_type, QueueWindowEventType.CONVERTED_TO_DRAFT)
        self.assertEqual(w.closed_by.timeline_event, ev)

    def test_pr_closed_closes_window_via_pr_field(self) -> None:
        """Trailing window is closed by PR_CLOSED when pr.closed_at is set."""
        rule_set = self._label_ruleset()
        pr = self._mk_pr(9)
        pr.closed_at = _dt(2024, 9, 7)
        pr.save(update_fields=["closed_at"])
        rules = rules_for_rule_set(rule_set)

        windows = _queue_windows_with_rules(pr, rules=rules, as_of=_dt(2024, 9, 10))

        self.assertEqual(len(windows), 1)
        w = windows[0]
        self.assertEqual(w.to_ts, _dt(2024, 9, 7))
        self.assertEqual(w.closed_by.event_type, QueueWindowEventType.PR_CLOSED)

    def test_open_window_has_null_closed_by(self) -> None:
        """The current open window has closed_by=None."""
        rule_set = self._label_ruleset()
        pr = self._mk_pr(10)
        rules = rules_for_rule_set(rule_set)

        windows = _queue_windows_with_rules(pr, rules=rules, as_of=_dt(2024, 9, 10))

        self.assertEqual(len(windows), 1)
        self.assertIsNone(windows[0].closed_by)
        self.assertIsNone(windows[0].to_ts)


# ---------------------------------------------------------------------------
# CI path
# ---------------------------------------------------------------------------


class TestCIPathAttribution(_Base):
    def test_ci_passed_via_check_run_opens_window(self) -> None:
        """Window opens when a check run flips CI to passing."""
        rule_set = self._ci_ruleset()
        pr = self._mk_pr(20)
        self._add_revision(pr, "sha1", _dt(2024, 9, 1), None, 0)
        cr = self._mk_check_run("sha1", node_id="CR_OK", conclusion="SUCCESS", ts=_dt(2024, 9, 4))
        rules = rules_for_rule_set(rule_set)

        windows = _queue_windows_with_rules(pr, rules=rules, as_of=_dt(2024, 9, 10))

        self.assertEqual(len(windows), 1)
        w = windows[0]
        self.assertEqual(w.opened_by.event_type, QueueWindowEventType.CI_PASSED)
        self.assertEqual(w.opened_by.check_run, cr)
        self.assertIsNone(w.opened_by.status_context)
        self.assertIsNone(w.closed_by)

    def test_ci_passed_via_status_context_opens_window(self) -> None:
        """Window opens when a status context flips CI to passing."""
        rule_set = self._ci_ruleset()
        pr = self._mk_pr(21)
        self._add_revision(pr, "sha1", _dt(2024, 9, 1), None, 0)
        sc = self._mk_status_context("sha1", node_id="SC_OK", state="SUCCESS", ts=_dt(2024, 9, 4))
        rules = rules_for_rule_set(rule_set)

        windows = _queue_windows_with_rules(pr, rules=rules, as_of=_dt(2024, 9, 10))

        self.assertEqual(len(windows), 1)
        w = windows[0]
        self.assertEqual(w.opened_by.event_type, QueueWindowEventType.CI_PASSED)
        self.assertEqual(w.opened_by.status_context, sc)
        self.assertIsNone(w.opened_by.check_run)

    def test_ci_failed_via_check_run_closes_window(self) -> None:
        """Window closes when a check run flips CI to failing."""
        rule_set = self._ci_ruleset()
        pr = self._mk_pr(22)
        self._add_revision(pr, "sha1", _dt(2024, 9, 1), None, 0)
        self._mk_check_run("sha1", node_id="CR_OK", conclusion="SUCCESS", ts=_dt(2024, 9, 3))
        cr_fail = self._mk_check_run("sha1", node_id="CR_FAIL", conclusion="FAILURE", ts=_dt(2024, 9, 7))
        rules = rules_for_rule_set(rule_set)

        windows = _queue_windows_with_rules(pr, rules=rules, as_of=_dt(2024, 9, 10))

        self.assertEqual(len(windows), 1)
        w = windows[0]
        self.assertEqual(w.closed_by.event_type, QueueWindowEventType.CI_FAILED)
        self.assertEqual(w.closed_by.check_run, cr_fail)

    def test_ci_failed_via_status_context_closes_window(self) -> None:
        """Window closes when a status context flips CI to failing."""
        rule_set = self._ci_ruleset()
        pr = self._mk_pr(23)
        self._add_revision(pr, "sha1", _dt(2024, 9, 1), None, 0)
        self._mk_check_run("sha1", node_id="CR_OK", conclusion="SUCCESS", ts=_dt(2024, 9, 3))
        sc_fail = self._mk_status_context("sha1", node_id="SC_FAIL", state="FAILURE", ts=_dt(2024, 9, 7))
        rules = rules_for_rule_set(rule_set)

        windows = _queue_windows_with_rules(pr, rules=rules, as_of=_dt(2024, 9, 10))

        self.assertEqual(len(windows), 1)
        w = windows[0]
        self.assertEqual(w.closed_by.event_type, QueueWindowEventType.CI_FAILED)
        self.assertEqual(w.closed_by.status_context, sc_fail)

    def test_head_sha_populated_from_revision(self) -> None:
        """opened_by.head_sha and closed_by.head_sha reflect the active revision SHA."""
        rule_set = self._ci_ruleset()
        pr = self._mk_pr(24)
        self._add_revision(pr, "sha1", _dt(2024, 9, 1), None, 0)
        self._mk_check_run("sha1", node_id="CR_OK", conclusion="SUCCESS", ts=_dt(2024, 9, 4))
        self._mk_check_run("sha1", node_id="CR_FAIL", conclusion="FAILURE", ts=_dt(2024, 9, 8))
        rules = rules_for_rule_set(rule_set)

        windows = _queue_windows_with_rules(pr, rules=rules, as_of=_dt(2024, 9, 10))

        self.assertEqual(len(windows), 1)
        w = windows[0]
        self.assertEqual(w.opened_by.head_sha, "sha1")
        self.assertEqual(w.closed_by.head_sha, "sha1")

    def test_head_sha_changes_across_revision_boundary(self) -> None:
        """opened_by.head_sha reflects the SHA at open; closed_by.head_sha reflects the SHA at close."""
        rule_set = self._ci_ruleset()
        pr = self._mk_pr(25)
        self._add_revision(pr, "sha1", _dt(2024, 9, 1), _dt(2024, 9, 6), 0)
        self._add_revision(pr, "sha2", _dt(2024, 9, 6), None, 1)
        # CI passes on sha1, then sha2 passes, then sha2 fails.
        self._mk_check_run("sha1", node_id="CR_sha1_OK", conclusion="SUCCESS", ts=_dt(2024, 9, 3))
        self._mk_check_run("sha2", node_id="CR_sha2_OK", conclusion="SUCCESS", ts=_dt(2024, 9, 7))
        self._mk_check_run("sha2", node_id="CR_sha2_FAIL", conclusion="FAILURE", ts=_dt(2024, 9, 9))
        rules = rules_for_rule_set(rule_set)

        windows = _queue_windows_with_rules(pr, rules=rules, as_of=_dt(2024, 9, 12))

        # Window 1: sha1 CI passes Sep 3, closes at revision boundary Sep 6.
        # Window 2: sha2 CI passes Sep 7, closes when sha2 fails Sep 9.
        self.assertEqual(len(windows), 2)
        self.assertEqual(windows[0].opened_by.head_sha, "sha1")
        self.assertEqual(windows[1].opened_by.head_sha, "sha2")
        self.assertEqual(windows[1].closed_by.head_sha, "sha2")

    def test_initial_state_synthetic_when_ci_gated_and_no_ci_yet(self) -> None:
        """No window at all when CI is required but not yet reported (INITIAL_STATE not used for CI path initial missing)."""
        rule_set = self._ci_ruleset()
        pr = self._mk_pr(26)
        self._add_revision(pr, "sha1", _dt(2024, 9, 1), None, 0)
        # No CI events at all.
        rules = rules_for_rule_set(rule_set)

        windows = _queue_windows_with_rules(pr, rules=rules, as_of=_dt(2024, 9, 10))

        self.assertEqual(len(windows), 0)

    def test_head_pushed_closes_window_at_revision_boundary(self) -> None:
        """Window closes at a revision boundary with HEAD_PUSHED attribution (no timeline FK)."""
        rule_set = self._ci_ruleset()
        pr = self._mk_pr(27)
        self._add_revision(pr, "sha1", _dt(2024, 9, 1), _dt(2024, 9, 6), 0)
        self._add_revision(pr, "sha2", _dt(2024, 9, 6), None, 1)
        self._mk_check_run("sha1", node_id="CR_sha1_OK", conclusion="SUCCESS", ts=_dt(2024, 9, 3))
        # No CI for sha2 so window stays closed after revision boundary.
        rules = rules_for_rule_set(rule_set)

        windows = _queue_windows_with_rules(pr, rules=rules, as_of=_dt(2024, 9, 10))

        self.assertEqual(len(windows), 1)
        w = windows[0]
        self.assertEqual(w.to_ts, _dt(2024, 9, 6))
        self.assertEqual(w.closed_by.event_type, QueueWindowEventType.HEAD_PUSHED)
        self.assertIsNone(w.closed_by.timeline_event)
        self.assertIsNone(w.closed_by.check_run)
        self.assertIsNone(w.closed_by.status_context)
