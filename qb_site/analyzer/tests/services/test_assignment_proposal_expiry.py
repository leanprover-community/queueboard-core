from __future__ import annotations

from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from analyzer.models import AssignmentProposal, QueueRuleSet, QueueSnapshot
from analyzer.services.assignment_proposal_expiry import expire_and_reconcile_proposals_for_repo
from analyzer.tasks.assignment_proposal_expiry import expire_assignment_proposals_task
from core.models import Repository
from syncer.models import PullRequest
from syncer.models.pull_request import PullRequestState


class ExpireAndReconcileProposalsTests(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        self.rule_set = QueueRuleSet.objects.create(repository=self.repo, version=1, is_default=True, is_active=True)
        self.cache_key = str(self.rule_set.id)
        self.now = timezone.now()

    def _proposal(self, pr_number, *, login="bob", state=AssignmentProposal.STATE_PROPOSED, expires_in_days=7):
        return AssignmentProposal.objects.create(
            repository=self.repo,
            pr_number=pr_number,
            reviewer_login=login,
            state=state,
            expires_at=self.now + timedelta(days=expires_in_days),
        )

    def _make_pr(self, number, *, state=PullRequestState.OPEN, assignees=None):
        return PullRequest.objects.create(
            repository=self.repo,
            number=number,
            state=state,
            is_draft=False,
            gh_created_at=self.now,
            gh_updated_at=self.now,
            base_ref_name="master",
            head_ref_name=f"branch-{number}",
            head_repo_owner_login="leanprover-community",
            head_repo_name="mathlib4",
            title=f"PR {number}",
            body="b",
            additions=1,
            deletions=0,
            changed_files_count=1,
            assignees=list(assignees or []),
        )

    def _make_queue_snapshot(self, *, queue, known, generated_at=None):
        QueueSnapshot.objects.create(
            repository=self.repo,
            cache_key=self.cache_key,
            generated_at=generated_at or self.now,
            payload={"lists": {"dashboards": {"Queue": list(queue)}}, "prs": {str(n): {} for n in known}},
            etag="etag",
            pr_count=len(known),
            queue_count=len(queue),
        )

    def _run(self):
        return expire_and_reconcile_proposals_for_repo(self.repo, now=self.now)

    # ---- transitions ---------------------------------------------------

    def test_time_expired_proposal_becomes_expired(self) -> None:
        proposal = self._proposal(101, expires_in_days=-1)
        self._make_pr(101)

        result = self._run()

        proposal.refresh_from_db()
        self.assertEqual(proposal.state, AssignmentProposal.STATE_EXPIRED)
        self.assertEqual(proposal.decided_via, AssignmentProposal.DECIDED_VIA_AUTO_EXPIRE)
        self.assertEqual(proposal.decided_at, self.now)
        self.assertEqual(result["stats"]["expired"], 1)

    def test_closed_pr_supersedes_proposal(self) -> None:
        proposal = self._proposal(102)
        self._make_pr(102, state=PullRequestState.CLOSED)

        result = self._run()

        proposal.refresh_from_db()
        self.assertEqual(proposal.state, AssignmentProposal.STATE_SUPERSEDED)
        self.assertEqual(proposal.decided_via, AssignmentProposal.DECIDED_VIA_SYNC_SUPERSEDED)
        self.assertEqual(result["stats"]["superseded"], 1)

    def test_human_assignee_supersedes_proposal(self) -> None:
        proposal = self._proposal(103)
        self._make_pr(103, assignees=["human"])

        result = self._run()

        proposal.refresh_from_db()
        self.assertEqual(proposal.state, AssignmentProposal.STATE_SUPERSEDED)
        self.assertEqual(result["stats"]["superseded"], 1)

    def test_live_proposal_untouched(self) -> None:
        proposal = self._proposal(104, expires_in_days=7)
        self._make_pr(104)
        self._make_queue_snapshot(queue=[104], known=[104])

        result = self._run()

        proposal.refresh_from_db()
        self.assertEqual(proposal.state, AssignmentProposal.STATE_PROPOSED)
        self.assertIsNone(proposal.decided_at)
        self.assertEqual(result["stats"]["still_live"], 1)

    def test_off_queue_invalidate_supersedes_with_fresh_snapshot(self) -> None:
        proposal = self._proposal(105)
        self._make_pr(105)
        # PR is known to the fresh snapshot but not on the Queue -> off-queue.
        self._make_queue_snapshot(queue=[999], known=[105, 999])

        result = self._run()

        proposal.refresh_from_db()
        self.assertEqual(proposal.state, AssignmentProposal.STATE_SUPERSEDED)
        self.assertEqual(result["stats"]["superseded"], 1)

    @override_settings(ANALYZER_ASSIGNMENT_PROPOSAL_ON_QUEUE_EXIT="retain")
    def test_off_queue_retain_keeps_proposal_live(self) -> None:
        proposal = self._proposal(106)
        self._make_pr(106)
        self._make_queue_snapshot(queue=[999], known=[106, 999])

        result = self._run()

        proposal.refresh_from_db()
        self.assertEqual(proposal.state, AssignmentProposal.STATE_PROPOSED)
        self.assertEqual(result["stats"]["still_live"], 1)

    def test_off_queue_ignored_when_snapshot_stale(self) -> None:
        # A stale snapshot must not be trusted for off-queue invalidation.
        proposal = self._proposal(107)
        self._make_pr(107)
        self._make_queue_snapshot(queue=[999], known=[107, 999], generated_at=self.now - timedelta(days=3))

        result = self._run()

        proposal.refresh_from_db()
        self.assertEqual(proposal.state, AssignmentProposal.STATE_PROPOSED)
        self.assertEqual(result["stats"]["still_live"], 1)

    def test_terminal_proposals_are_not_touched(self) -> None:
        # Only 'proposed' rows are considered; terminal history is left alone.
        declined = self._proposal(108, state=AssignmentProposal.STATE_DECLINED, expires_in_days=-5)
        self._make_pr(108, state=PullRequestState.CLOSED)

        result = self._run()

        declined.refresh_from_db()
        self.assertEqual(declined.state, AssignmentProposal.STATE_DECLINED)
        self.assertEqual(result["stats"]["active"], 0)


class ExpireAssignmentProposalsTaskTests(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        self.now = timezone.now()

    def _make_pr(self, number, *, state=PullRequestState.OPEN):
        return PullRequest.objects.create(
            repository=self.repo,
            number=number,
            state=state,
            is_draft=False,
            gh_created_at=self.now,
            gh_updated_at=self.now,
            base_ref_name="master",
            head_ref_name=f"branch-{number}",
            head_repo_owner_login="leanprover-community",
            head_repo_name="mathlib4",
            title=f"PR {number}",
            body="b",
            additions=1,
            deletions=0,
            changed_files_count=1,
            assignees=[],
        )

    def test_task_expires_across_active_repos(self) -> None:
        # Ungated: the sweep runs regardless of the master switch.
        AssignmentProposal.objects.create(
            repository=self.repo,
            pr_number=101,
            reviewer_login="bob",
            state=AssignmentProposal.STATE_PROPOSED,
            expires_at=self.now - timedelta(days=1),
        )
        self._make_pr(101)

        res = expire_assignment_proposals_task.apply().get()

        self.assertFalse(res["skipped"])
        self.assertEqual(res["totals"]["expired"], 1)
        proposal = AssignmentProposal.objects.get(repository=self.repo, pr_number=101)
        self.assertEqual(proposal.state, AssignmentProposal.STATE_EXPIRED)

    def test_task_repo_filter_miss(self) -> None:
        res = expire_assignment_proposals_task.apply(kwargs={"repository_id": 999999}).get()

        self.assertTrue(res["skipped"])
        self.assertEqual(res["reason"], "repo_not_found_or_inactive")
