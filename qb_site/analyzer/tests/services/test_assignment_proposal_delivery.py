from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

from django.test import TestCase, override_settings
from django.utils import timezone

from analyzer.models import AssignmentProposal
from analyzer.services.assignment_proposal_delivery import deliver_assignment_proposals
from core.models import Repository, User
from syncer.models import PullRequest
from syncer.models.pull_request import PullRequestState
from zulip_bot.services.zulip_client import ZulipApiError


@override_settings(QUEUEBOARD_BASE_URL="https://queue.example.org")
class DeliverAssignmentProposalsTests(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        self.other_repo = Repository.objects.create(owner="leanprover-community", name="batteries", default_branch="main")
        self.now = timezone.now()
        self._zulip_seq = 5000

    # ---- helpers -------------------------------------------------------

    def _make_user(self, login: str, *, reachable: bool = True) -> User:
        zulip_user_id = None
        if reachable:
            self._zulip_seq += 1
            zulip_user_id = self._zulip_seq
        return User.objects.create(github_login=login, zulip_user_id=zulip_user_id)

    def _make_pr(self, repo: Repository, number: int, *, title: str = "") -> PullRequest:
        return PullRequest.objects.create(
            repository=repo,
            number=number,
            state=PullRequestState.OPEN,
            is_draft=False,
            gh_created_at=self.now,
            gh_updated_at=self.now,
            base_ref_name="master",
            head_ref_name=f"branch-{number}",
            head_repo_owner_login=repo.owner,
            head_repo_name=repo.name,
            title=title or f"PR {number}",
            body="b",
            additions=1,
            deletions=0,
            changed_files_count=1,
            assignees=[],
        )

    def _make_proposal(self, repo: Repository, number: int, login: str, *, notified: bool = False, days: int = 7):
        return AssignmentProposal.objects.create(
            repository=repo,
            pr_number=number,
            reviewer_login=login,
            state=AssignmentProposal.STATE_PROPOSED,
            expires_at=self.now + timedelta(days=days),
            notified_at=self.now if notified else None,
        )

    def _deliver(self, *, enabled=True, dry_run=False, client=None):
        if client is None:
            client = MagicMock()
        result = deliver_assignment_proposals(
            [self.repo, self.other_repo],
            now=self.now,
            enabled=enabled,
            dry_run=dry_run,
            client=client,
        )
        return result, client

    # ---- happy path ----------------------------------------------------

    def test_sends_one_digest_per_reviewer_across_repos_and_stamps_notified_at(self) -> None:
        bob = self._make_user("bob")
        self._make_pr(self.repo, 101, title="Fix ring lemma")
        self._make_pr(self.other_repo, 202, title="Batteries tweak")
        p1 = self._make_proposal(self.repo, 101, "bob")
        p2 = self._make_proposal(self.other_repo, 202, "bob")

        result, client = self._deliver()

        # One DM to the reviewer, covering both repos.
        client.send_direct_message.assert_called_once()
        _, kwargs = client.send_direct_message.call_args
        self.assertEqual(kwargs["to"], [int(bob.zulip_user_id)])
        content = kwargs["content"]
        self.assertIn("https://queue.example.org/console/", content)
        self.assertIn("#101", content)
        self.assertIn("Fix ring lemma", content)
        self.assertIn("#202", content)

        self.assertEqual(result["stats"]["sent"], 1)
        self.assertEqual(result["stats"]["reviewers"], 1)
        self.assertEqual(result["stats"]["proposals_notified"], 2)
        p1.refresh_from_db()
        p2.refresh_from_db()
        self.assertEqual(p1.notified_at, self.now)
        self.assertEqual(p2.notified_at, self.now)

    def test_distinct_reviewers_get_separate_dms(self) -> None:
        self._make_user("bob")
        self._make_user("carol")
        self._make_proposal(self.repo, 101, "bob")
        self._make_proposal(self.repo, 102, "carol")

        result, client = self._deliver()

        self.assertEqual(client.send_direct_message.call_count, 2)
        self.assertEqual(result["stats"]["sent"], 2)
        self.assertEqual(result["stats"]["reviewers"], 2)

    # ---- dedupe via notified_at ---------------------------------------

    def test_already_notified_proposals_are_skipped(self) -> None:
        self._make_user("bob")
        self._make_proposal(self.repo, 101, "bob", notified=True)

        result, client = self._deliver()

        client.send_direct_message.assert_not_called()
        self.assertEqual(result["stats"]["pending_proposals"], 0)
        self.assertEqual(result["stats"]["sent"], 0)

    def test_rerun_is_idempotent(self) -> None:
        self._make_user("bob")
        self._make_proposal(self.repo, 101, "bob")

        first, client1 = self._deliver()
        self.assertEqual(first["stats"]["sent"], 1)

        second, client2 = self._deliver()
        client2.send_direct_message.assert_not_called()
        self.assertEqual(second["stats"]["sent"], 0)
        self.assertEqual(second["stats"]["pending_proposals"], 0)

    def test_new_proposal_next_cycle_triggers_a_fresh_digest(self) -> None:
        self._make_user("bob")
        self._make_proposal(self.repo, 101, "bob")
        first, _ = self._deliver()
        self.assertEqual(first["stats"]["sent"], 1)

        # A new proposal appears (notified_at NULL); next run sends again.
        self._make_proposal(self.repo, 102, "bob")
        second, client2 = self._deliver()
        client2.send_direct_message.assert_called_once()
        self.assertEqual(second["stats"]["sent"], 1)
        self.assertEqual(second["stats"]["proposals_notified"], 1)

    # ---- reachability / gating ----------------------------------------

    def test_unreachable_reviewer_is_skipped_without_stamping(self) -> None:
        self._make_user("bob", reachable=False)
        p1 = self._make_proposal(self.repo, 101, "bob")

        result, client = self._deliver()

        client.send_direct_message.assert_not_called()
        self.assertEqual(result["stats"]["skipped_no_zulip_user_id"], 1)
        p1.refresh_from_db()
        self.assertIsNone(p1.notified_at)

    def test_unknown_login_is_skipped(self) -> None:
        # A proposal whose login has no core.User row at all.
        self._make_proposal(self.repo, 101, "ghost")

        result, client = self._deliver()

        client.send_direct_message.assert_not_called()
        self.assertEqual(result["stats"]["skipped_no_user"], 1)

    def test_login_matched_case_insensitively(self) -> None:
        bob = self._make_user("Bob")
        self._make_proposal(self.repo, 101, "bob")

        result, client = self._deliver()

        client.send_direct_message.assert_called_once()
        _, kwargs = client.send_direct_message.call_args
        self.assertEqual(kwargs["to"], [int(bob.zulip_user_id)])
        self.assertEqual(result["stats"]["sent"], 1)

    def test_dry_run_sends_nothing_and_does_not_stamp(self) -> None:
        self._make_user("bob")
        p1 = self._make_proposal(self.repo, 101, "bob")

        result, client = self._deliver(dry_run=True)

        client.send_direct_message.assert_not_called()
        self.assertEqual(result["stats"]["would_send"], 1)
        self.assertEqual(result["stats"]["sent"], 0)
        p1.refresh_from_db()
        self.assertIsNone(p1.notified_at)

    def test_disabled_sends_nothing(self) -> None:
        self._make_user("bob")
        self._make_proposal(self.repo, 101, "bob")

        result, client = self._deliver(enabled=False, dry_run=False)

        client.send_direct_message.assert_not_called()
        self.assertEqual(result["stats"]["skipped_disabled"], 1)

    # ---- failures / robustness ----------------------------------------

    def test_send_failure_leaves_notified_at_null(self) -> None:
        self._make_user("bob")
        p1 = self._make_proposal(self.repo, 101, "bob")
        client = MagicMock()
        client.send_direct_message.side_effect = ZulipApiError("boom")

        result, _ = self._deliver(client=client)

        self.assertEqual(result["stats"]["failed"], 1)
        self.assertEqual(result["stats"]["sent"], 0)
        p1.refresh_from_db()
        self.assertIsNone(p1.notified_at)

    def test_none_client_when_enabled_counts_failed(self) -> None:
        self._make_user("bob")
        self._make_proposal(self.repo, 101, "bob")

        result = deliver_assignment_proposals([self.repo], now=self.now, enabled=True, dry_run=False, client=None)

        self.assertEqual(result["stats"]["failed"], 1)
        self.assertEqual(result["stats"]["sent"], 0)

    def test_only_proposed_state_is_delivered(self) -> None:
        self._make_user("bob")
        AssignmentProposal.objects.create(
            repository=self.repo,
            pr_number=101,
            reviewer_login="bob",
            state=AssignmentProposal.STATE_ACCEPTED,
            expires_at=self.now + timedelta(days=7),
        )

        result, client = self._deliver()

        client.send_direct_message.assert_not_called()
        self.assertEqual(result["stats"]["pending_proposals"], 0)

    def test_no_repos_is_noop(self) -> None:
        result = deliver_assignment_proposals([], now=self.now, enabled=True, dry_run=False, client=MagicMock())
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["stats"]["pending_proposals"], 0)
