from __future__ import annotations

from datetime import timedelta

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from analyzer.models import AssignmentProposal
from core.models import Repository


class AssignmentProposalModelTest(TestCase):
    def setUp(self):
        self.repo = Repository.objects.create(
            owner="leanprover-community", name="mathlib4", default_branch="master", is_active=True
        )

    def _make(self, *, pr_number=1, reviewer_login="rev", state=AssignmentProposal.STATE_PROPOSED):
        return AssignmentProposal.objects.create(
            repository=self.repo,
            pr_number=pr_number,
            reviewer_login=reviewer_login,
            state=state,
            expires_at=timezone.now() + timedelta(days=7),
        )

    def test_defaults(self):
        p = self._make()
        self.assertEqual(p.state, AssignmentProposal.STATE_PROPOSED)
        self.assertEqual(p.decided_via, "")
        self.assertIsNone(p.decided_at)
        self.assertIsNone(p.notified_at)
        self.assertIsNotNone(p.created_at)  # created_at is the proposal time

    def test_one_active_proposal_per_pr_enforced(self):
        self._make(pr_number=100, reviewer_login="alice")
        # A second *proposed* proposal for the same PR (even a different reviewer) is rejected
        # by the partial unique constraint — this is the "one at a time" invariant.
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._make(pr_number=100, reviewer_login="bob")

    def test_terminal_states_do_not_block_a_new_proposal(self):
        # An expired proposal must not block re-proposing (advance to the next candidate),
        # since the partial unique only covers state='proposed'.
        self._make(pr_number=200, reviewer_login="alice", state=AssignmentProposal.STATE_EXPIRED)
        p2 = self._make(pr_number=200, reviewer_login="bob")
        self.assertEqual(p2.state, AssignmentProposal.STATE_PROPOSED)
        # Terminal rows accumulate as history for the same PR.
        self._make(pr_number=200, reviewer_login="alice", state=AssignmentProposal.STATE_DECLINED)
        self.assertEqual(AssignmentProposal.objects.filter(repository=self.repo, pr_number=200).count(), 3)

    def test_active_proposals_for_different_prs_allowed(self):
        self._make(pr_number=301, reviewer_login="alice")
        self._make(pr_number=302, reviewer_login="alice")
        self.assertEqual(AssignmentProposal.objects.filter(state=AssignmentProposal.STATE_PROPOSED).count(), 2)
