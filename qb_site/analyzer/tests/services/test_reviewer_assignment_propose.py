from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

from django.test import TestCase
from django.utils import timezone

from analyzer.models import (
    AssignmentProposal,
    QueueRuleSet,
    ReviewerAssignmentApplication,
    ReviewerAssignmentSnapshot,
    ReviewerOptOut,
)
from analyzer.services.reviewer_assignment_propose import propose_assignments_for_repo
from core.models import Repository, ReviewerPreference, User
from syncer.models import PullRequest
from syncer.models.pull_request import PullRequestState


class ProposeAssignmentsForRepoTests(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        self.rule_set = QueueRuleSet.objects.create(repository=self.repo, version=1, is_default=True, is_active=True)
        self.cache_key = str(self.rule_set.id)
        self.now = timezone.now()
        self.run_date = self.now.date()
        self._zulip_seq = 1000

    # ---- helpers -------------------------------------------------------

    def _make_reviewer(
        self,
        login: str,
        *,
        acceptance: str = ReviewerPreference.ACCEPTANCE_CONFIRM,
        reachable: bool = True,
        auto_assign: bool = True,
    ) -> User:
        zulip_user_id = None
        if reachable:
            self._zulip_seq += 1
            zulip_user_id = self._zulip_seq
        user = User.objects.create(github_login=login, zulip_user_id=zulip_user_id)
        ReviewerPreference.objects.create(
            repository=self.repo,
            user=user,
            auto_assign=auto_assign,
            assignment_acceptance=acceptance,
        )
        return user

    def _make_pr(self, number: int, *, assignees=None, state=PullRequestState.OPEN) -> PullRequest:
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

    def _make_snapshot(self, assignments: dict[int, str], *, cache_key=None, generated_at=None) -> ReviewerAssignmentSnapshot:
        return ReviewerAssignmentSnapshot.objects.create(
            repository=self.repo,
            cache_key=cache_key or self.cache_key,
            generated_at=generated_at or self.now,
            payload={"meta": {}, "automatic_assignments": {str(k): v for k, v in assignments.items()}},
            etag="etag",
            assignment_count=len(assignments),
        )

    def _propose(
        self,
        *,
        enabled=True,
        dry_run=False,
        window_days=7,
        max_per_repo=0,
        dedupe_days=7,
        max_age_hours=48,
        client=None,
        token="tok",
    ):
        if client is None:
            client = MagicMock()
            client.assign.side_effect = lambda **kwargs: (kwargs["github_login"],)
        sync = MagicMock()
        result = propose_assignments_for_repo(
            self.repo,
            run_date=self.run_date,
            now=self.now,
            enabled=enabled,
            dry_run=dry_run,
            window_days=window_days,
            dedupe_days=dedupe_days,
            max_age_hours=max_age_hours,
            max_per_repo=max_per_repo,
            token_resolver=lambda **kwargs: token,
            assignment_client=client,
            sync_enqueuer=sync,
        )
        return result, client, sync

    # ---- per-mode branching -------------------------------------------

    def test_auto_mode_direct_assigns(self) -> None:
        self._make_reviewer("alice", acceptance=ReviewerPreference.ACCEPTANCE_AUTO, reachable=False)
        self._make_snapshot({101: "alice"})
        self._make_pr(101)

        result, client, sync = self._propose()

        self.assertEqual(result["stats"]["assigned_auto"], 1)
        self.assertEqual(result["stats"]["proposed"], 0)
        client.assign.assert_called_once_with(owner="leanprover-community", repo="mathlib4", number=101, github_login="alice")
        sync.assert_called_once_with("leanprover-community", "mathlib4", 101)
        self.assertFalse(AssignmentProposal.objects.filter(repository=self.repo, pr_number=101).exists())
        record = ReviewerAssignmentApplication.objects.get(repository=self.repo, pr_number=101, reviewer_login="alice")
        self.assertEqual(record.status, ReviewerAssignmentApplication.STATUS_APPLIED)

    def test_confirm_reachable_creates_proposal(self) -> None:
        self._make_reviewer("bob", acceptance=ReviewerPreference.ACCEPTANCE_CONFIRM, reachable=True)
        snapshot = self._make_snapshot({102: "bob"})
        self._make_pr(102)

        result, client, sync = self._propose(window_days=7)

        self.assertEqual(result["stats"]["proposed"], 1)
        self.assertEqual(result["stats"]["assigned_auto"], 0)
        client.assign.assert_not_called()
        sync.assert_not_called()
        proposal = AssignmentProposal.objects.get(repository=self.repo, pr_number=102)
        self.assertEqual(proposal.state, AssignmentProposal.STATE_PROPOSED)
        self.assertEqual(proposal.reviewer_login, "bob")
        self.assertEqual(proposal.snapshot_id, snapshot.id)
        self.assertEqual(proposal.expires_at, self.now + timedelta(days=7))
        # A proposal is never a GitHub assignment: no application row.
        self.assertFalse(ReviewerAssignmentApplication.objects.filter(repository=self.repo, pr_number=102).exists())

    def test_confirm_unreachable_falls_back_to_direct_assign(self) -> None:
        self._make_reviewer("carol", acceptance=ReviewerPreference.ACCEPTANCE_CONFIRM, reachable=False)
        self._make_snapshot({103: "carol"})
        self._make_pr(103)

        result, client, sync = self._propose()

        self.assertEqual(result["stats"]["assigned_fallback"], 1)
        self.assertEqual(result["stats"]["proposed"], 0)
        client.assign.assert_called_once()
        self.assertFalse(AssignmentProposal.objects.filter(repository=self.repo, pr_number=103).exists())

    def test_per_reviewer_window_override_clamped_to_seven(self) -> None:
        user = self._make_reviewer("bob", acceptance=ReviewerPreference.ACCEPTANCE_CONFIRM, reachable=True)
        pref = ReviewerPreference.objects.get(repository=self.repo, user=user)
        pref.notification_settings = {"assignment_proposal_window_days": 3}  # below the >=7 floor
        pref.save(update_fields=["notification_settings"])
        self._make_snapshot({104: "bob"})
        self._make_pr(104)

        self._propose(window_days=7)

        proposal = AssignmentProposal.objects.get(repository=self.repo, pr_number=104)
        self.assertEqual(proposal.expires_at, self.now + timedelta(days=7))

    def test_global_window_below_seven_is_honored(self) -> None:
        # The >=7 clamp applies only to per-reviewer overrides; the operator-configured global
        # ANALYZER_ASSIGNMENT_PROPOSAL_WINDOW_DAYS is honored as-is (base.py/.env.example document
        # the clamp as per-reviewer-only).
        self._make_reviewer("bob", acceptance=ReviewerPreference.ACCEPTANCE_CONFIRM, reachable=True)
        self._make_snapshot({106: "bob"})
        self._make_pr(106)

        self._propose(window_days=3)

        proposal = AssignmentProposal.objects.get(repository=self.repo, pr_number=106)
        self.assertEqual(proposal.expires_at, self.now + timedelta(days=3))

    def test_per_reviewer_window_override_larger_is_honored(self) -> None:
        user = self._make_reviewer("bob", acceptance=ReviewerPreference.ACCEPTANCE_CONFIRM, reachable=True)
        pref = ReviewerPreference.objects.get(repository=self.repo, user=user)
        pref.notification_settings = {"assignment_proposal_window_days": 14}
        pref.save(update_fields=["notification_settings"])
        self._make_snapshot({105: "bob"})
        self._make_pr(105)

        self._propose(window_days=7)

        proposal = AssignmentProposal.objects.get(repository=self.repo, pr_number=105)
        self.assertEqual(proposal.expires_at, self.now + timedelta(days=14))

    # ---- dry-run / gating ---------------------------------------------

    def test_dry_run_has_no_side_effects(self) -> None:
        self._make_reviewer("alice", acceptance=ReviewerPreference.ACCEPTANCE_AUTO, reachable=False)
        self._make_reviewer("bob", acceptance=ReviewerPreference.ACCEPTANCE_CONFIRM, reachable=True)
        self._make_snapshot({101: "alice", 102: "bob"})
        self._make_pr(101)
        self._make_pr(102)

        result, client, sync = self._propose(dry_run=True)

        self.assertEqual(result["stats"]["skipped_dry_run"], 2)
        self.assertEqual(result["stats"]["proposed"], 0)
        self.assertEqual(result["stats"]["assigned_auto"], 0)
        client.assign.assert_not_called()
        sync.assert_not_called()
        self.assertFalse(AssignmentProposal.objects.filter(repository=self.repo).exists())
        self.assertFalse(ReviewerAssignmentApplication.objects.filter(repository=self.repo).exists())

    def test_disabled_creates_nothing(self) -> None:
        self._make_reviewer("bob", acceptance=ReviewerPreference.ACCEPTANCE_CONFIRM, reachable=True)
        self._make_snapshot({102: "bob"})
        self._make_pr(102)

        result, _client, _sync = self._propose(enabled=False, dry_run=False)

        self.assertEqual(result["stats"]["skipped_disabled"], 1)
        self.assertFalse(AssignmentProposal.objects.filter(repository=self.repo).exists())

    def test_no_token_skips_direct_assign_but_proposals_still_created(self) -> None:
        # A missing assign token blocks direct-assign, but DB-only proposals need no token.
        self._make_reviewer("alice", acceptance=ReviewerPreference.ACCEPTANCE_AUTO, reachable=False)
        self._make_reviewer("bob", acceptance=ReviewerPreference.ACCEPTANCE_CONFIRM, reachable=True)
        self._make_snapshot({101: "alice", 102: "bob"})
        self._make_pr(101)
        self._make_pr(102)

        result, client, _sync = self._propose(token=None)

        self.assertEqual(result["stats"]["skipped_no_token"], 1)
        self.assertEqual(result["stats"]["proposed"], 1)
        client.assign.assert_not_called()
        self.assertTrue(AssignmentProposal.objects.filter(repository=self.repo, pr_number=102).exists())

    # ---- re-validation / idempotency ----------------------------------

    def test_skips_pr_with_existing_active_proposal(self) -> None:
        self._make_reviewer("bob", acceptance=ReviewerPreference.ACCEPTANCE_CONFIRM, reachable=True)
        self._make_snapshot({102: "bob"})
        self._make_pr(102)
        AssignmentProposal.objects.create(
            repository=self.repo,
            pr_number=102,
            reviewer_login="dave",  # a different, earlier candidate
            state=AssignmentProposal.STATE_PROPOSED,
            expires_at=self.now + timedelta(days=7),
        )

        result, _client, _sync = self._propose()

        self.assertEqual(result["stats"]["skipped_already_proposed"], 1)
        self.assertEqual(result["stats"]["proposed"], 0)
        # Still exactly one active proposal for the PR (the pre-existing one).
        self.assertEqual(
            AssignmentProposal.objects.filter(
                repository=self.repo, pr_number=102, state=AssignmentProposal.STATE_PROPOSED
            ).count(),
            1,
        )

    def test_skips_already_assigned_and_opted_out_and_ineligible(self) -> None:
        self._make_reviewer("alice", acceptance=ReviewerPreference.ACCEPTANCE_AUTO, reachable=False)
        self._make_reviewer("bob", acceptance=ReviewerPreference.ACCEPTANCE_CONFIRM, reachable=True)
        self._make_snapshot({101: "alice", 102: "bob", 103: "carol"})
        self._make_pr(101, assignees=["alice"])  # already assigned
        self._make_pr(102)
        ReviewerOptOut.objects.create(
            repository=self.repo, pr_number=102, reviewer_login="bob", active=True, opted_out_at=self.now
        )  # opted out
        self._make_pr(103)  # carol has no ReviewerPreference -> ineligible

        result, client, _sync = self._propose()

        self.assertEqual(result["stats"]["skipped_already_assigned"], 1)
        self.assertEqual(result["stats"]["skipped_opted_out"], 1)
        self.assertEqual(result["stats"]["skipped_ineligible"], 1)
        self.assertEqual(result["stats"]["proposed"], 0)
        client.assign.assert_not_called()

    def test_any_assignee_blocks_proposal_even_a_non_reviewer(self) -> None:
        # Mirror proposal_validity: ANY assignee supersedes, not just eligible reviewers. Creating
        # a proposal here would have it superseded by the next expiry sweep and re-created (and
        # re-DM'd) by the next propose run — the daily-churn loop from the design doc 050 review.
        self._make_reviewer("bob", acceptance=ReviewerPreference.ACCEPTANCE_CONFIRM, reachable=True)
        self._make_snapshot({101: "bob"})
        self._make_pr(101, assignees=["some-bot"])  # assignee is not an eligible reviewer

        result, client, _sync = self._propose()

        self.assertEqual(result["stats"]["skipped_already_assigned"], 1)
        self.assertEqual(result["stats"]["proposed"], 0)
        self.assertFalse(AssignmentProposal.objects.exists())
        client.assign.assert_not_called()

    def test_recently_applied_skips_direct_assign(self) -> None:
        self._make_reviewer("alice", acceptance=ReviewerPreference.ACCEPTANCE_AUTO, reachable=False)
        self._make_snapshot({101: "alice"})
        self._make_pr(101)
        ReviewerAssignmentApplication.objects.create(
            run_date=self.run_date - timedelta(days=1),
            repository=self.repo,
            pr_number=101,
            reviewer_login="alice",
            status=ReviewerAssignmentApplication.STATUS_APPLIED,
            applied_at=self.now - timedelta(days=1),
        )

        result, client, _sync = self._propose(dedupe_days=7)

        self.assertEqual(result["stats"]["skipped_recently_applied"], 1)
        client.assign.assert_not_called()

    def test_already_recorded_same_day_application_is_skipped(self) -> None:
        # A same-day FAILED application is not "recently applied" (that only counts APPLIED), so the
        # direct-assign helper's get_or_create finds the existing row and reports already_recorded.
        self._make_reviewer("alice", acceptance=ReviewerPreference.ACCEPTANCE_AUTO, reachable=False)
        self._make_snapshot({101: "alice"})
        self._make_pr(101)
        ReviewerAssignmentApplication.objects.create(
            run_date=self.run_date,
            repository=self.repo,
            pr_number=101,
            reviewer_login="alice",
            status=ReviewerAssignmentApplication.STATUS_FAILED,
        )

        result, client, _sync = self._propose()

        self.assertEqual(result["stats"]["skipped_already_recorded"], 1)
        self.assertEqual(result["stats"]["assigned_auto"], 0)
        client.assign.assert_not_called()

    def test_per_repo_cap_bounds_direct_assigns_but_not_proposals(self) -> None:
        # PRs are processed in ascending number order: 101/102 auto, 103 confirm.
        self._make_reviewer("alice", acceptance=ReviewerPreference.ACCEPTANCE_AUTO, reachable=False)
        self._make_reviewer("bob", acceptance=ReviewerPreference.ACCEPTANCE_CONFIRM, reachable=True)
        self._make_snapshot({101: "alice", 102: "alice", 103: "bob"})
        self._make_pr(101)
        self._make_pr(102)
        self._make_pr(103)

        result, client, _sync = self._propose(max_per_repo=1)

        self.assertEqual(result["stats"]["assigned_auto"], 1)
        self.assertTrue(result["stats"]["capped"])
        self.assertEqual(result["stats"]["capped_remaining"], 1)
        self.assertEqual(client.assign.call_count, 1)
        # The cap defers only GitHub mutations; the confirm-mode proposal is still created.
        self.assertEqual(result["stats"]["proposed"], 1)
        self.assertTrue(AssignmentProposal.objects.filter(repository=self.repo, pr_number=103).exists())

    def test_rerun_is_idempotent(self) -> None:
        self._make_reviewer("alice", acceptance=ReviewerPreference.ACCEPTANCE_AUTO, reachable=False)
        self._make_reviewer("bob", acceptance=ReviewerPreference.ACCEPTANCE_CONFIRM, reachable=True)
        self._make_snapshot({101: "alice", 102: "bob"})
        self._make_pr(101)
        self._make_pr(102)

        first, _c1, _s1 = self._propose()
        self.assertEqual(first["stats"]["assigned_auto"], 1)
        self.assertEqual(first["stats"]["proposed"], 1)

        second, c2, s2 = self._propose()
        # PR 101 was applied within the dedupe window (recently-applied wins over the same-day
        # already-recorded guard); PR 102 already has an active proposal.
        self.assertEqual(second["stats"]["assigned_auto"], 0)
        self.assertEqual(second["stats"]["proposed"], 0)
        self.assertEqual(second["stats"]["skipped_recently_applied"], 1)
        self.assertEqual(second["stats"]["skipped_already_proposed"], 1)
        c2.assign.assert_not_called()
        s2.assert_not_called()
        self.assertEqual(AssignmentProposal.objects.filter(repository=self.repo, pr_number=102, state="proposed").count(), 1)

    def test_no_snapshot_skipped(self) -> None:
        self._make_reviewer("bob", acceptance=ReviewerPreference.ACCEPTANCE_CONFIRM, reachable=True)
        self._make_snapshot({102: "bob"}, cache_key="default")  # wrong cache key
        self._make_pr(102)

        result, _client, _sync = self._propose()

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "no_snapshot")

    def test_stale_snapshot_skipped(self) -> None:
        self._make_reviewer("bob", acceptance=ReviewerPreference.ACCEPTANCE_CONFIRM, reachable=True)
        self._make_snapshot({102: "bob"}, generated_at=self.now - timedelta(days=3))
        self._make_pr(102)

        result, _client, _sync = self._propose(max_age_hours=48)

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "stale_snapshot")
        self.assertFalse(AssignmentProposal.objects.filter(repository=self.repo).exists())
