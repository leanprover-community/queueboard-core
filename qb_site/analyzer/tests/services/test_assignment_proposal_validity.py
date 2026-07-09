from __future__ import annotations

from datetime import datetime, timedelta, timezone

from django.test import SimpleTestCase

from analyzer.models import AssignmentProposal
from analyzer.services.assignment_proposal_validity import (
    ON_QUEUE_EXIT_INVALIDATE,
    ON_QUEUE_EXIT_RETAIN,
    REASON_ALREADY_TERMINAL,
    REASON_EXPIRED,
    REASON_LIVE,
    REASON_PR_ASSIGNED,
    REASON_PR_CLOSED,
    REASON_PR_OFF_QUEUE,
    proposal_validity,
)


class ProposalValidityTests(SimpleTestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 1, 15, tzinfo=timezone.utc)

    def _proposal(self, *, state=AssignmentProposal.STATE_PROPOSED, expires_in_days: float = 7) -> AssignmentProposal:
        # Unsaved instance — proposal_validity is a pure predicate over durable facts.
        return AssignmentProposal(
            pr_number=1,
            reviewer_login="alice",
            state=state,
            expires_at=self.now + timedelta(days=expires_in_days),
        )

    def test_live_when_open_on_queue_unexpired_unassigned(self) -> None:
        v = proposal_validity(
            self._proposal(),
            now=self.now,
            pr_state="open",
            current_assignees=set(),
            on_queue=True,
            on_queue_exit=ON_QUEUE_EXIT_INVALIDATE,
        )
        self.assertTrue(v.is_live)
        self.assertEqual(v.reason, REASON_LIVE)
        self.assertIsNone(v.terminal_state)

    def test_expired_when_past_window(self) -> None:
        v = proposal_validity(
            self._proposal(expires_in_days=-1),
            now=self.now,
            pr_state="open",
            current_assignees=set(),
            on_queue=True,
            on_queue_exit=ON_QUEUE_EXIT_INVALIDATE,
        )
        self.assertFalse(v.is_live)
        self.assertEqual(v.reason, REASON_EXPIRED)
        self.assertEqual(v.terminal_state, AssignmentProposal.STATE_EXPIRED)
        self.assertEqual(v.decided_via, AssignmentProposal.DECIDED_VIA_AUTO_EXPIRE)

    def test_superseded_when_closed(self) -> None:
        for state in ("closed", "merged"):
            v = proposal_validity(
                self._proposal(),
                now=self.now,
                pr_state=state,
                current_assignees=set(),
                on_queue=True,
                on_queue_exit=ON_QUEUE_EXIT_INVALIDATE,
            )
            self.assertFalse(v.is_live, state)
            self.assertEqual(v.reason, REASON_PR_CLOSED)
            self.assertEqual(v.terminal_state, AssignmentProposal.STATE_SUPERSEDED)
            self.assertEqual(v.decided_via, AssignmentProposal.DECIDED_VIA_SYNC_SUPERSEDED)

    def test_superseded_when_assignee_landed(self) -> None:
        # A human/self-assignee landing supersedes even an unexpired, on-queue proposal.
        v = proposal_validity(
            self._proposal(),
            now=self.now,
            pr_state="open",
            current_assignees={"human"},
            on_queue=True,
            on_queue_exit=ON_QUEUE_EXIT_INVALIDATE,
        )
        self.assertFalse(v.is_live)
        self.assertEqual(v.reason, REASON_PR_ASSIGNED)
        self.assertEqual(v.terminal_state, AssignmentProposal.STATE_SUPERSEDED)

    def test_assignee_takes_precedence_over_expiry(self) -> None:
        v = proposal_validity(
            self._proposal(expires_in_days=-5),
            now=self.now,
            pr_state="open",
            current_assignees={"human"},
            on_queue=True,
        )
        self.assertEqual(v.reason, REASON_PR_ASSIGNED)

    def test_closed_takes_precedence_over_expiry(self) -> None:
        v = proposal_validity(
            self._proposal(expires_in_days=-5),
            now=self.now,
            pr_state="closed",
            current_assignees=set(),
            on_queue=None,
        )
        self.assertEqual(v.reason, REASON_PR_CLOSED)

    def test_off_queue_invalidate_supersedes(self) -> None:
        v = proposal_validity(
            self._proposal(),
            now=self.now,
            pr_state="open",
            current_assignees=set(),
            on_queue=False,
            on_queue_exit=ON_QUEUE_EXIT_INVALIDATE,
        )
        self.assertFalse(v.is_live)
        self.assertEqual(v.reason, REASON_PR_OFF_QUEUE)
        self.assertEqual(v.terminal_state, AssignmentProposal.STATE_SUPERSEDED)

    def test_off_queue_retain_stays_live(self) -> None:
        v = proposal_validity(
            self._proposal(),
            now=self.now,
            pr_state="open",
            current_assignees=set(),
            on_queue=False,
            on_queue_exit=ON_QUEUE_EXIT_RETAIN,
        )
        self.assertTrue(v.is_live)
        self.assertEqual(v.reason, REASON_LIVE)

    def test_unknown_queue_membership_does_not_invalidate(self) -> None:
        # on_queue=None (no fresh snapshot) must not supersede an otherwise-live proposal.
        v = proposal_validity(
            self._proposal(),
            now=self.now,
            pr_state="open",
            current_assignees=set(),
            on_queue=None,
            on_queue_exit=ON_QUEUE_EXIT_INVALIDATE,
        )
        self.assertTrue(v.is_live)

    def test_terminal_proposal_is_not_live_and_needs_no_transition(self) -> None:
        for state in (
            AssignmentProposal.STATE_ACCEPTED,
            AssignmentProposal.STATE_DECLINED,
            AssignmentProposal.STATE_EXPIRED,
            AssignmentProposal.STATE_SUPERSEDED,
        ):
            v = proposal_validity(
                self._proposal(state=state, expires_in_days=-10),
                now=self.now,
                pr_state="closed",
                current_assignees={"human"},
                on_queue=False,
            )
            self.assertFalse(v.is_live, state)
            self.assertEqual(v.reason, REASON_ALREADY_TERMINAL)
            self.assertIsNone(v.terminal_state)
