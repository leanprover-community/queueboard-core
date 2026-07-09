from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone

from django.test import TestCase

from analyzer.models import AssignmentProposal, PRQueueWindow, QueueRuleSet
from analyzer.services.reviewer_attention import build_reviewer_attention_reports
from core.models import Repository, ReviewerPreference, User
from syncer.models import PullRequest, PRTimelineEvent, PRTimelineEventType


def _dt(year: int, month: int, day: int, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=dt_timezone.utc)


class ReviewerAttentionServiceTests(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        self.rules = QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            require_open=True,
            require_not_draft=True,
            require_ci_success=False,
            required_label_names=[],
            forbidden_label_names=[],
            is_active=True,
        )
        self.reviewer = User.objects.create(github_login="alice")
        self.pref = ReviewerPreference.objects.create(
            repository=self.repo,
            user=self.reviewer,
            notifications_enabled=True,
            notification_settings={"stale_nudge_days": 14, "auto_unassign_days": 21},
        )
        self.now = _dt(2026, 2, 23, 12)

    def _mk_pr(self, number: int, *, assignees: list[str]) -> PullRequest:
        return PullRequest.objects.create(
            repository=self.repo,
            number=number,
            author=None,
            state="open",
            is_draft=False,
            gh_created_at=self.now - timedelta(days=50),
            gh_updated_at=self.now,
            base_ref_name="master",
            head_ref_name=f"branch-{number}",
            head_repo_owner_login="leanprover-community",
            head_repo_name="mathlib4",
            title=f"PR {number}",
            body="",
            additions=0,
            deletions=0,
            changed_files_count=0,
            assignees=assignees,
            approvals=[],
            commenters=[],
            files=[],
        )

    def _add_assignment_event(self, *, pr: PullRequest, assignee_login: str, occurred_at: datetime) -> None:
        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.ASSIGNED,
            occurred_at=occurred_at,
            assignee_login=assignee_login,
        )

    def _add_active_window(self, *, pr: PullRequest, from_ts: datetime) -> None:
        PRQueueWindow.objects.create(
            pull_request=pr,
            rule_set=self.rules,
            from_ts=from_ts,
            to_ts=None,
            cycle_index=0,
            duration_seconds_closed=0,
            cumulative_seconds_closed=0,
            window_count=1,
            first_on_queue_ts=from_ts,
        )

    def test_flags_nudge_when_between_x_and_y(self) -> None:
        pr = self._mk_pr(101, assignees=["alice"])
        self._add_assignment_event(pr=pr, assignee_login="alice", occurred_at=self.now - timedelta(days=20))
        self._add_active_window(pr=pr, from_ts=self.now - timedelta(days=20))

        reports = build_reviewer_attention_reports(repository=self.repo, as_of=self.now)
        report = reports[0]
        item = report.items[0]

        self.assertTrue(item.needs_nudge)
        self.assertFalse(item.needs_auto_unassign)
        self.assertEqual(item.days_on_queue_since_assignment, 20)
        self.assertEqual(item.total_queue_days, 20)
        self.assertEqual(item.total_queue_seconds, 20 * 24 * 60 * 60)
        self.assertTrue(report.has_events_of_interest)
        self.assertTrue(report.has_notifications_to_send)

    def test_flags_auto_unassign_when_at_or_after_y(self) -> None:
        pr = self._mk_pr(102, assignees=["alice"])
        self._add_assignment_event(pr=pr, assignee_login="alice", occurred_at=self.now - timedelta(days=21))
        self._add_active_window(pr=pr, from_ts=self.now - timedelta(days=21))

        reports = build_reviewer_attention_reports(repository=self.repo, as_of=self.now)
        item = reports[0].items[0]

        self.assertFalse(item.needs_nudge)
        self.assertTrue(item.needs_auto_unassign)
        self.assertEqual(item.days_on_queue_since_assignment, 21)

    def test_auto_unassign_flag_still_computed_when_notifications_disabled(self) -> None:
        self.pref.notifications_enabled = False
        self.pref.save(update_fields=["notifications_enabled"])

        pr = self._mk_pr(103, assignees=["alice"])
        self._add_assignment_event(pr=pr, assignee_login="alice", occurred_at=self.now - timedelta(days=30))
        self._add_active_window(pr=pr, from_ts=self.now - timedelta(days=30))

        reports = build_reviewer_attention_reports(repository=self.repo, as_of=self.now)
        report = reports[0]
        item = report.items[0]

        self.assertTrue(item.needs_auto_unassign)
        self.assertTrue(report.has_events_of_interest)
        self.assertFalse(report.has_notifications_to_send)

    def test_missing_assignment_timestamp_emits_warning_and_no_actions(self) -> None:
        pr = self._mk_pr(104, assignees=["alice"])
        self._add_active_window(pr=pr, from_ts=self.now - timedelta(days=16))

        reports = build_reviewer_attention_reports(repository=self.repo, as_of=self.now)
        report = reports[0]
        item = report.items[0]

        self.assertTrue(item.is_on_queue)
        self.assertTrue(item.missing_assignment_timestamp)
        self.assertFalse(item.needs_nudge)
        self.assertFalse(item.needs_auto_unassign)
        self.assertIn("Missing assignment timestamp for PR #104.", report.warnings)

    def test_queue_anchor_uses_active_window_start_after_reentry(self) -> None:
        pr = self._mk_pr(105, assignees=["alice"])
        self._add_assignment_event(pr=pr, assignee_login="alice", occurred_at=self.now - timedelta(days=40))
        # Closed past cycle (10 days) plus current active cycle (5 days).
        PRQueueWindow.objects.create(
            pull_request=pr,
            rule_set=self.rules,
            from_ts=self.now - timedelta(days=30),
            to_ts=self.now - timedelta(days=20),
            cycle_index=0,
            duration_seconds_closed=10 * 24 * 60 * 60,
            cumulative_seconds_closed=10 * 24 * 60 * 60,
            window_count=1,
            first_on_queue_ts=self.now - timedelta(days=30),
        )
        self._add_active_window(pr=pr, from_ts=self.now - timedelta(days=5))

        reports = build_reviewer_attention_reports(repository=self.repo, as_of=self.now)
        item = reports[0].items[0]

        self.assertEqual(item.days_on_queue_since_assignment, 5)
        self.assertEqual(item.total_queue_days, 15)
        self.assertEqual(item.total_queue_seconds, 15 * 24 * 60 * 60)
        self.assertFalse(item.needs_nudge)
        self.assertFalse(item.needs_auto_unassign)

    def test_total_queue_fields_are_none_when_no_queue_windows(self) -> None:
        pr = self._mk_pr(106, assignees=["alice"])
        self._add_assignment_event(pr=pr, assignee_login="alice", occurred_at=self.now - timedelta(days=2))

        reports = build_reviewer_attention_reports(repository=self.repo, as_of=self.now)
        report = reports[0]
        item = next(i for i in report.items if i.pr_number == pr.number)

        self.assertFalse(item.is_on_queue)
        self.assertIsNone(item.total_queue_days)
        self.assertIsNone(item.total_queue_seconds)

    def test_flags_new_assignment_ping_within_window(self) -> None:
        pr = self._mk_pr(107, assignees=["alice"])
        self._add_assignment_event(pr=pr, assignee_login="alice", occurred_at=self.now - timedelta(hours=12))

        reports = build_reviewer_attention_reports(
            repository=self.repo,
            as_of=self.now,
            new_assignment_ping_window_seconds=24 * 60 * 60,
        )
        report = reports[0]
        item = report.items[0]

        self.assertTrue(item.needs_new_assignment_ping)
        self.assertFalse(item.needs_nudge)
        self.assertFalse(item.needs_auto_unassign)
        self.assertTrue(report.has_events_of_interest)
        self.assertTrue(report.has_notifications_to_send)

    def test_does_not_flag_new_assignment_ping_after_window(self) -> None:
        pr = self._mk_pr(108, assignees=["alice"])
        self._add_assignment_event(pr=pr, assignee_login="alice", occurred_at=self.now - timedelta(hours=26))

        reports = build_reviewer_attention_reports(
            repository=self.repo,
            as_of=self.now,
            new_assignment_ping_window_seconds=24 * 60 * 60,
        )
        report = reports[0]
        item = report.items[0]

        self.assertFalse(item.needs_new_assignment_ping)

    def test_policy_start_at_caps_queue_age_for_flags(self) -> None:
        pr = self._mk_pr(109, assignees=["alice"])
        self._add_assignment_event(pr=pr, assignee_login="alice", occurred_at=self.now - timedelta(days=30))
        self._add_active_window(pr=pr, from_ts=self.now - timedelta(days=30))
        policy_start_at = self.now - timedelta(days=5)

        reports = build_reviewer_attention_reports(
            repository=self.repo,
            as_of=self.now,
            policy_start_at=policy_start_at,
        )
        item = reports[0].items[0]

        self.assertEqual(item.last_assigned_at, self.now - timedelta(days=30))
        self.assertEqual(item.days_on_queue_since_assignment, 5)
        self.assertFalse(item.needs_nudge)
        self.assertFalse(item.needs_auto_unassign)

    def test_policy_start_at_suppresses_new_assignment_ping_before_floor(self) -> None:
        pr = self._mk_pr(110, assignees=["alice"])
        assigned_at = self.now - timedelta(hours=12)
        self._add_assignment_event(pr=pr, assignee_login="alice", occurred_at=assigned_at)
        policy_start_at = self.now - timedelta(hours=6)

        reports = build_reviewer_attention_reports(
            repository=self.repo,
            as_of=self.now,
            new_assignment_ping_window_seconds=24 * 60 * 60,
            policy_start_at=policy_start_at,
        )
        item = reports[0].items[0]

        self.assertFalse(item.needs_new_assignment_ping)

    def _make_accepted_proposal(self, *, pr_number: int, reviewer_login: str, decided_at: datetime, decided_via: str) -> None:
        AssignmentProposal.objects.create(
            repository=self.repo,
            pr_number=pr_number,
            reviewer_login=reviewer_login,
            state=AssignmentProposal.STATE_ACCEPTED,
            expires_at=self.now - timedelta(days=1),
            decided_at=decided_at,
            decided_via=decided_via,
        )

    def test_new_assignment_ping_suppressed_after_console_accept(self) -> None:
        # design doc 050 Chunk 5: the proposal DM already covered this assignment, so the attention
        # sweep must not also ping "newly assigned" for it.
        pr = self._mk_pr(111, assignees=["alice"])
        self._add_assignment_event(pr=pr, assignee_login="alice", occurred_at=self.now - timedelta(hours=2))
        self._make_accepted_proposal(
            pr_number=111,
            reviewer_login="Alice",  # case-insensitive match against the login
            decided_at=self.now - timedelta(hours=2),
            decided_via=AssignmentProposal.DECIDED_VIA_CONSOLE,
        )

        reports = build_reviewer_attention_reports(
            repository=self.repo,
            as_of=self.now,
            new_assignment_ping_window_seconds=24 * 60 * 60,
        )
        item = reports[0].items[0]

        self.assertFalse(item.needs_new_assignment_ping)

    def test_new_assignment_ping_not_suppressed_by_old_or_non_console_accept(self) -> None:
        # An accept outside the ping window, or one not made via the console (e.g. a direct assign),
        # does not suppress the ping.
        pr_old = self._mk_pr(112, assignees=["alice"])
        self._add_assignment_event(pr=pr_old, assignee_login="alice", occurred_at=self.now - timedelta(hours=2))
        self._make_accepted_proposal(
            pr_number=112,
            reviewer_login="alice",
            decided_at=self.now - timedelta(days=3),  # before the 24h ping window
            decided_via=AssignmentProposal.DECIDED_VIA_CONSOLE,
        )
        pr_noncon = self._mk_pr(113, assignees=["alice"])
        self._add_assignment_event(pr=pr_noncon, assignee_login="alice", occurred_at=self.now - timedelta(hours=2))
        self._make_accepted_proposal(
            pr_number=113,
            reviewer_login="alice",
            decided_at=self.now - timedelta(hours=2),
            decided_via=AssignmentProposal.DECIDED_VIA_SYNC_SUPERSEDED,
        )

        reports = build_reviewer_attention_reports(
            repository=self.repo,
            as_of=self.now,
            new_assignment_ping_window_seconds=24 * 60 * 60,
        )
        items = {item.pr_number: item for item in reports[0].items}

        self.assertTrue(items[112].needs_new_assignment_ping)
        self.assertTrue(items[113].needs_new_assignment_ping)
