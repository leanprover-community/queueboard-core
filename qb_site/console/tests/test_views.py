from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from analyzer.models import AssignmentProposal, QueueSnapshot, ReviewerAssignmentApplication, ReviewerOptOut
from console.session import SESSION_NONCE_KEY, SESSION_USER_KEY
from core.models import Repository, ReviewerPreference, User
from core.services.github_assignment import AssignmentMutationError
from core.services.github_oauth import GitHubOAuthError, GitHubUserIdentity
from core.services.oauth_state import ConsoleOAuthStateClaims, issue_console_oauth_state
from syncer.models import PullRequest
from syncer.models.pull_request import PullRequestState


class ConsoleViewTests(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        self.now = timezone.now()
        self.reviewer = User.objects.create(github_login="bob", github_node_id="node-bob", zulip_user_id=7001)
        ReviewerPreference.objects.create(
            repository=self.repo,
            user=self.reviewer,
            preferred_labels=["t-analysis"],
            maximum_capacity=5,
            assignment_acceptance=ReviewerPreference.ACCEPTANCE_CONFIRM,
        )

    # ---- helpers -------------------------------------------------------

    def _login_session(self, user: User | None = None) -> None:
        session = self.client.session
        session[SESSION_USER_KEY] = int((user or self.reviewer).id)
        session.save()

    def _make_pr(self, number: int, *, state=PullRequestState.OPEN, assignees=None) -> PullRequest:
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

    def _proposal(self, number: int, *, login="bob", state=AssignmentProposal.STATE_PROPOSED) -> AssignmentProposal:
        return AssignmentProposal.objects.create(
            repository=self.repo,
            pr_number=number,
            reviewer_login=login,
            state=state,
            expires_at=self.now + timedelta(days=7),
        )

    def _seed_snapshot(self, repo=None, prs=None) -> None:
        """Seed the cached queue snapshot the load helper reads (cache_key 'default' with no ruleset)."""
        repo = repo or self.repo
        payload_prs = prs or {}
        QueueSnapshot.objects.create(
            repository=repo,
            cache_key="default",
            generated_at=self.now,
            payload={"prs": payload_prs},
            etag="etag",
            pr_count=len(payload_prs),
            queue_count=0,
        )

    # ---- auth / session ------------------------------------------------

    def test_home_without_session_shows_login(self) -> None:
        resp = self.client.get(reverse("console:home"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Sign in with GitHub")

    def test_login_redirects_to_github(self) -> None:
        fake = MagicMock()
        fake.build_authorize_url.return_value = "https://github.com/login/oauth/authorize?state=xyz"
        with patch("console.views.GitHubOAuthClient", return_value=fake):
            resp = self.client.get(reverse("console:login"))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], "https://github.com/login/oauth/authorize?state=xyz")
        # A CSRF nonce was stashed in the session.
        self.assertTrue(self.client.session.get(SESSION_NONCE_KEY))

    def test_login_when_oauth_unconfigured(self) -> None:
        with patch("console.views.GitHubOAuthClient", side_effect=GitHubOAuthError("no creds")):
            resp = self.client.get(reverse("console:login"))
        self.assertEqual(resp.status_code, 503)

    def test_oauth_callback_happy_path_opens_session(self) -> None:
        # Seed the session nonce as the login step would.
        session = self.client.session
        session[SESSION_NONCE_KEY] = "nonce-123"
        session.save()
        state = issue_console_oauth_state(claims=ConsoleOAuthStateClaims(nonce="nonce-123", next="/console/"))

        fake = MagicMock()
        fake.exchange_code_for_access_token.return_value = "gho_token"
        fake.fetch_user_identity.return_value = GitHubUserIdentity(
            github_user_id=1, github_node_id="node-bob", github_login="bob", github_name="Bob", github_avatar_url=None
        )
        with patch("console.views.GitHubOAuthClient", return_value=fake):
            resp = self.client.get(reverse("console:oauth-callback"), {"code": "c", "state": state})

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], "/console/")
        self.assertEqual(self.client.session.get(SESSION_USER_KEY), self.reviewer.id)

    def test_oauth_callback_rotates_session_key(self) -> None:
        # Session fixation: the pre-login session key (which an attacker could have planted) must
        # not remain valid once the session is promoted to an authenticated reviewer session.
        session = self.client.session
        session[SESSION_NONCE_KEY] = "nonce-123"
        session.save()
        pre_login_key = session.session_key
        state = issue_console_oauth_state(claims=ConsoleOAuthStateClaims(nonce="nonce-123", next="/console/"))

        fake = MagicMock()
        fake.exchange_code_for_access_token.return_value = "gho_token"
        fake.fetch_user_identity.return_value = GitHubUserIdentity(
            github_user_id=1, github_node_id="node-bob", github_login="bob", github_name="Bob", github_avatar_url=None
        )
        with patch("console.views.GitHubOAuthClient", return_value=fake):
            resp = self.client.get(reverse("console:oauth-callback"), {"code": "c", "state": state})

        self.assertEqual(resp.status_code, 302)
        self.assertNotEqual(self.client.session.session_key, pre_login_key)
        self.assertEqual(self.client.session.get(SESSION_USER_KEY), self.reviewer.id)

    def test_oauth_callback_recycled_login_denied(self) -> None:
        # The reviewer's old login now belongs to a different GitHub account (different node id);
        # that account must not inherit the reviewer's console session.
        session = self.client.session
        session[SESSION_NONCE_KEY] = "nonce-123"
        session.save()
        state = issue_console_oauth_state(claims=ConsoleOAuthStateClaims(nonce="nonce-123", next="/console/"))

        fake = MagicMock()
        fake.exchange_code_for_access_token.return_value = "gho_token"
        fake.fetch_user_identity.return_value = GitHubUserIdentity(
            github_user_id=999,
            github_node_id="node-imposter",
            github_login=self.reviewer.github_login,
            github_name="Imposter",
            github_avatar_url=None,
        )
        with patch("console.views.GitHubOAuthClient", return_value=fake):
            resp = self.client.get(reverse("console:oauth-callback"), {"code": "c", "state": state})

        self.assertEqual(resp.status_code, 403)
        self.assertIsNone(self.client.session.get(SESSION_USER_KEY))

    def test_oauth_callback_unknown_login_denied(self) -> None:
        # A GitHub account we have never seen must not get a session or mint a core.User row
        # (design doc 050 review): the console is for already-registered reviewers only.
        session = self.client.session
        session[SESSION_NONCE_KEY] = "nonce-123"
        session.save()
        state = issue_console_oauth_state(claims=ConsoleOAuthStateClaims(nonce="nonce-123", next="/console/"))

        fake = MagicMock()
        fake.exchange_code_for_access_token.return_value = "gho_token"
        fake.fetch_user_identity.return_value = GitHubUserIdentity(
            github_user_id=999,
            github_node_id="node-stranger",
            github_login="stranger",
            github_name="Stranger",
            github_avatar_url=None,
        )
        with patch("console.views.GitHubOAuthClient", return_value=fake):
            resp = self.client.get(reverse("console:oauth-callback"), {"code": "c", "state": state})

        self.assertEqual(resp.status_code, 403)
        self.assertContains(resp, "registered reviewers", status_code=403)
        self.assertIsNone(self.client.session.get(SESSION_USER_KEY))
        self.assertFalse(User.objects.filter(github_login__iexact="stranger").exists())

    def test_oauth_callback_nonce_mismatch_rejected(self) -> None:
        session = self.client.session
        session[SESSION_NONCE_KEY] = "session-nonce"
        session.save()
        state = issue_console_oauth_state(claims=ConsoleOAuthStateClaims(nonce="different-nonce", next="/console/"))
        resp = self.client.get(reverse("console:oauth-callback"), {"code": "c", "state": state})
        self.assertEqual(resp.status_code, 400)
        self.assertIsNone(self.client.session.get(SESSION_USER_KEY))

    # ---- list view -----------------------------------------------------

    def test_home_lists_pending_proposals(self) -> None:
        self._make_pr(101)
        self._proposal(101)
        self._seed_snapshot()  # no assigned PRs; the pending proposal alone is 1.0 of load
        self._login_session()

        resp = self.client.get(reverse("console:home"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "leanprover-community/mathlib4")  # per-repo heading
        self.assertContains(resp, "#101")
        # Weighted load, incl. the pending proposal (1.0), against capacity 5.
        self.assertContains(resp, "Load: 1 / 5 (4 free)")
        self.assertContains(resp, "Accept")
        self.assertContains(resp, "Decline")

    def test_home_shows_load_and_assigned_prs_without_proposals(self) -> None:
        # A reviewer with an assigned PR but no proposals still gets a repo section: load + roster.
        self._make_pr(200, assignees=["bob"])
        self._seed_snapshot(prs={"200": {"assignees": ["bob"], "author": "alice", "pr_status": "AwaitingReview"}})
        self._login_session()

        resp = self.client.get(reverse("console:home"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "leanprover-community/mathlib4")
        self.assertContains(resp, "Load: 1 / 5 (4 free)")
        self.assertContains(resp, "Assigned to you (1)")
        self.assertContains(resp, "#200")
        self.assertNotContains(resp, "Accept")  # no proposals

    def test_home_empty_state_when_nothing(self) -> None:
        self._seed_snapshot()
        self._login_session()

        resp = self.client.get(reverse("console:home"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "no proposals awaiting acceptance, and no PRs currently assigned")

    def test_home_groups_proposals_by_repo(self) -> None:
        # Proposals across repos render under separate per-repo headings, each with its own load
        # line, ordered by (owner, name) — so "batteries" sorts before "mathlib4" (doc 050 review).
        repo2 = Repository.objects.create(owner="leanprover-community", name="batteries", default_branch="main")
        ReviewerPreference.objects.create(
            repository=repo2,
            user=self.reviewer,
            preferred_labels=[],
            maximum_capacity=3,
            assignment_acceptance=ReviewerPreference.ACCEPTANCE_CONFIRM,
        )
        self._make_pr(101)
        self._proposal(101)
        PullRequest.objects.create(
            repository=repo2,
            number=5,
            state=PullRequestState.OPEN,
            is_draft=False,
            gh_created_at=self.now,
            gh_updated_at=self.now,
            base_ref_name="main",
            head_ref_name="branch-5",
            head_repo_owner_login="leanprover-community",
            head_repo_name="batteries",
            title="Array lemmas",
            body="b",
            additions=1,
            deletions=0,
            changed_files_count=1,
            assignees=[],
        )
        AssignmentProposal.objects.create(
            repository=repo2,
            pr_number=5,
            reviewer_login="bob",
            state=AssignmentProposal.STATE_PROPOSED,
            expires_at=self.now + timedelta(days=7),
        )
        self._seed_snapshot()
        self._seed_snapshot(repo=repo2)
        self._login_session()

        resp = self.client.get(reverse("console:home"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "leanprover-community/mathlib4")
        self.assertContains(resp, "leanprover-community/batteries")
        self.assertContains(resp, "Load: 1 / 5 (4 free)")  # mathlib4: one pending proposal
        self.assertContains(resp, "Load: 1 / 3 (2 free)")  # batteries: one pending proposal
        content = resp.content.decode()
        self.assertLess(content.index("batteries"), content.index("mathlib4"))

    # ---- accept --------------------------------------------------------

    @override_settings(ANALYZER_ASSIGNMENT_PROPOSALS_ASSIGN_ON_ACCEPT_ENABLED=False)
    def test_accept_when_assign_on_accept_disabled(self) -> None:
        self._make_pr(101)
        proposal = self._proposal(101)
        self._login_session()

        resp = self.client.post(reverse("console:accept", args=[proposal.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "enabled yet")
        proposal.refresh_from_db()
        self.assertEqual(proposal.state, AssignmentProposal.STATE_PROPOSED)

    @override_settings(ANALYZER_ASSIGNMENT_PROPOSALS_ASSIGN_ON_ACCEPT_ENABLED=True)
    def test_accept_assigns_and_marks_accepted(self) -> None:
        self._make_pr(101)
        proposal = self._proposal(101)
        self._login_session()

        with (
            patch("core.services.github_operation_tokens.resolve_github_app_operation_token", return_value="tok"),
            patch("console.views.assign_reviewer_and_record", return_value=("applied", None, None)) as mock_assign,
        ):
            resp = self.client.post(reverse("console:accept", args=[proposal.id]))

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Assignment accepted")
        mock_assign.assert_called_once()
        proposal.refresh_from_db()
        self.assertEqual(proposal.state, AssignmentProposal.STATE_ACCEPTED)
        self.assertEqual(proposal.decided_via, AssignmentProposal.DECIDED_VIA_CONSOLE)
        self.assertIsNotNone(proposal.decided_at)

    @override_settings(ANALYZER_ASSIGNMENT_PROPOSALS_ASSIGN_ON_ACCEPT_ENABLED=True)
    def test_accept_failed_outcome_leaves_proposal_pending(self) -> None:
        self._make_pr(101)
        proposal = self._proposal(101)
        self._login_session()

        with (
            patch("core.services.github_operation_tokens.resolve_github_app_operation_token", return_value="tok"),
            patch("console.views.assign_reviewer_and_record", return_value=("failed", None, None)) as mock_assign,
        ):
            resp = self.client.post(reverse("console:accept", args=[proposal.id]))

        self.assertEqual(resp.status_code, 502)
        mock_assign.assert_called_once()
        proposal.refresh_from_db()
        self.assertEqual(proposal.state, AssignmentProposal.STATE_PROPOSED)

    @override_settings(ANALYZER_ASSIGNMENT_PROPOSALS_ASSIGN_ON_ACCEPT_ENABLED=True)
    def test_accept_already_recorded_failed_row_is_not_marked_accepted(self) -> None:
        # Regression (design doc 050 review): a prior same-day FAILED application returns
        # already_recorded, but the assignment never landed — the reviewer must NOT be told they are
        # assigned, and the proposal must stay pending for a later retry.
        self._make_pr(101)
        proposal = self._proposal(101)
        self._login_session()
        failed_record = ReviewerAssignmentApplication(status=ReviewerAssignmentApplication.STATUS_FAILED)

        with (
            patch("core.services.github_operation_tokens.resolve_github_app_operation_token", return_value="tok"),
            patch("console.views.assign_reviewer_and_record", return_value=("already_recorded", None, failed_record)),
        ):
            resp = self.client.post(reverse("console:accept", args=[proposal.id]))

        self.assertEqual(resp.status_code, 502)
        proposal.refresh_from_db()
        self.assertEqual(proposal.state, AssignmentProposal.STATE_PROPOSED)

    @override_settings(ANALYZER_ASSIGNMENT_PROPOSALS_ASSIGN_ON_ACCEPT_ENABLED=True)
    def test_accept_already_recorded_applied_row_marks_accepted(self) -> None:
        # Benign double-accept: the row already exists and is APPLIED, so acceptance is idempotent.
        self._make_pr(101)
        proposal = self._proposal(101)
        self._login_session()
        applied_record = ReviewerAssignmentApplication(status=ReviewerAssignmentApplication.STATUS_APPLIED)

        with (
            patch("core.services.github_operation_tokens.resolve_github_app_operation_token", return_value="tok"),
            patch("console.views.assign_reviewer_and_record", return_value=("already_recorded", None, applied_record)),
        ):
            resp = self.client.post(reverse("console:accept", args=[proposal.id]))

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Assignment accepted")
        proposal.refresh_from_db()
        self.assertEqual(proposal.state, AssignmentProposal.STATE_ACCEPTED)

    @override_settings(ANALYZER_ASSIGNMENT_PROPOSALS_ASSIGN_ON_ACCEPT_ENABLED=True)
    def test_accept_stale_closed_pr_renders_unavailable_and_retires(self) -> None:
        self._make_pr(101, state=PullRequestState.CLOSED)
        proposal = self._proposal(101)
        self._login_session()

        with patch("console.views.assign_reviewer_and_record") as mock_assign:
            resp = self.client.post(reverse("console:accept", args=[proposal.id]))

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "closed or merged")
        mock_assign.assert_not_called()
        proposal.refresh_from_db()
        self.assertEqual(proposal.state, AssignmentProposal.STATE_SUPERSEDED)

    @override_settings(ANALYZER_ASSIGNMENT_PROPOSALS_ASSIGN_ON_ACCEPT_ENABLED=True)
    def test_accept_proposal_for_other_reviewer_forbidden(self) -> None:
        self._make_pr(101)
        proposal = self._proposal(101, login="carol")
        self._login_session()  # signed in as bob

        resp = self.client.post(reverse("console:accept", args=[proposal.id]))
        self.assertEqual(resp.status_code, 403)
        proposal.refresh_from_db()
        self.assertEqual(proposal.state, AssignmentProposal.STATE_PROPOSED)

    @override_settings(ANALYZER_ASSIGNMENT_PROPOSALS_ASSIGN_ON_ACCEPT_ENABLED=True)
    def test_accept_with_active_opt_out_supersedes_proposal(self) -> None:
        # Regression (design doc 050 review): an active opt-out on the PR blocks acceptance, and the
        # dangling proposal is retired to superseded rather than left pending. Opt-outs feed the
        # shared proposal_validity predicate, so the expiry sweep retires these the same way.
        self._make_pr(101)
        proposal = self._proposal(101)
        ReviewerOptOut.objects.create(
            repository=self.repo, pr_number=101, reviewer_login="bob", active=True, opted_out_at=self.now
        )
        self._login_session()

        with patch("console.views.assign_reviewer_and_record") as mock_assign:
            resp = self.client.post(reverse("console:accept", args=[proposal.id]))

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "opted out")
        mock_assign.assert_not_called()
        proposal.refresh_from_db()
        self.assertEqual(proposal.state, AssignmentProposal.STATE_SUPERSEDED)
        self.assertEqual(proposal.decided_via, AssignmentProposal.DECIDED_VIA_SYNC_SUPERSEDED)

    def test_accept_requires_post(self) -> None:
        self._make_pr(101)
        proposal = self._proposal(101)
        self._login_session()
        resp = self.client.get(reverse("console:accept", args=[proposal.id]))
        self.assertEqual(resp.status_code, 405)

    def test_accept_without_session_redirects_to_login(self) -> None:
        self._make_pr(101)
        proposal = self._proposal(101)
        resp = self.client.post(reverse("console:accept", args=[proposal.id]))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("console:login"), resp["Location"])

    # ---- decline -------------------------------------------------------

    def test_decline_marks_declined_and_opts_out(self) -> None:
        self._make_pr(101)
        proposal = self._proposal(101)
        self._login_session()

        resp = self.client.post(reverse("console:decline", args=[proposal.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "declined")
        proposal.refresh_from_db()
        self.assertEqual(proposal.state, AssignmentProposal.STATE_DECLINED)
        self.assertEqual(proposal.decided_via, AssignmentProposal.DECIDED_VIA_CONSOLE)
        opt_out = ReviewerOptOut.objects.get(repository=self.repo, pr_number=101, reviewer_login="bob")
        self.assertTrue(opt_out.active)

    def test_decline_lowercases_opt_out_login(self) -> None:
        # Proposals carry GitHub's canonical (mixed-case) login; the opt-out row must be written
        # lowercase like every other writer so the syncer's exact-match clearing can find it.
        mixed_case = User.objects.create(github_login="YaelDillies", github_node_id="node-yael", zulip_user_id=7002)
        self._make_pr(102)
        proposal = self._proposal(102, login="YaelDillies")
        self._login_session(mixed_case)

        resp = self.client.post(reverse("console:decline", args=[proposal.id]))
        self.assertEqual(resp.status_code, 200)
        opt_out = ReviewerOptOut.objects.get(repository=self.repo, pr_number=102)
        self.assertEqual(opt_out.reviewer_login, "yaeldillies")
        self.assertTrue(opt_out.active)

    def test_decline_already_terminal_renders_unavailable(self) -> None:
        self._make_pr(101)
        proposal = self._proposal(101, state=AssignmentProposal.STATE_ACCEPTED)
        self._login_session()

        resp = self.client.post(reverse("console:decline", args=[proposal.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "already been decided")
        self.assertFalse(ReviewerOptOut.objects.filter(repository=self.repo, pr_number=101).exists())

    # ---- assign anyway -------------------------------------------------

    def test_expired_proposal_accept_offers_assign_anyway(self) -> None:
        # A reviewer clicks Accept after the proposal lapsed: the unavailable page invites them to
        # self-assign, since the PR is still open and unassigned.
        self._make_pr(101)
        proposal = self._proposal(101)
        AssignmentProposal.objects.filter(id=proposal.id).update(expires_at=self.now - timedelta(days=1))
        self._login_session()

        resp = self.client.post(reverse("console:accept", args=[proposal.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Assign myself anyway")
        # The "no longer available" page links out to the GitHub PR.
        self.assertContains(resp, "https://github.com/leanprover-community/mathlib4/pull/101")

    @override_settings(ANALYZER_ASSIGNMENT_PROPOSALS_ASSIGN_ON_ACCEPT_ENABLED=True)
    def test_assign_anyway_assigns_and_clears_opt_out(self) -> None:
        # A declined proposal the reviewer changed their mind on: self-assign lands and the opt-out
        # the decline created is retracted so the builder won't undo it.
        self._make_pr(101)  # open, unassigned
        proposal = self._proposal(101, state=AssignmentProposal.STATE_DECLINED)
        ReviewerOptOut.objects.create(
            repository=self.repo, pr_number=101, reviewer_login="bob", active=True, opted_out_at=self.now
        )
        self._login_session()

        with (
            patch("core.services.github_operation_tokens.resolve_github_app_operation_token", return_value="tok"),
            patch("console.views.assign_reviewer_and_record", return_value=("applied", None, None)) as mock_assign,
        ):
            resp = self.client.post(reverse("console:assign-anyway", args=[proposal.id]))

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Assignment accepted")
        mock_assign.assert_called_once()
        opt_out = ReviewerOptOut.objects.get(repository=self.repo, pr_number=101, reviewer_login="bob")
        self.assertFalse(opt_out.active)
        self.assertIsNotNone(opt_out.cleared_at)

    @override_settings(ANALYZER_ASSIGNMENT_PROPOSALS_ASSIGN_ON_ACCEPT_ENABLED=False)
    def test_assign_anyway_disabled_does_not_assign(self) -> None:
        self._make_pr(101)
        proposal = self._proposal(101, state=AssignmentProposal.STATE_EXPIRED)
        self._login_session()

        with patch("console.views.assign_reviewer_and_record") as mock_assign:
            resp = self.client.post(reverse("console:assign-anyway", args=[proposal.id]))

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "enabled yet")
        mock_assign.assert_not_called()

    @override_settings(ANALYZER_ASSIGNMENT_PROPOSALS_ASSIGN_ON_ACCEPT_ENABLED=True)
    def test_assign_anyway_closed_pr_rejected(self) -> None:
        self._make_pr(101, state=PullRequestState.CLOSED)
        proposal = self._proposal(101, state=AssignmentProposal.STATE_SUPERSEDED)
        self._login_session()

        with patch("console.views.assign_reviewer_and_record") as mock_assign:
            resp = self.client.post(reverse("console:assign-anyway", args=[proposal.id]))

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "closed or merged")
        mock_assign.assert_not_called()

    @override_settings(ANALYZER_ASSIGNMENT_PROPOSALS_ASSIGN_ON_ACCEPT_ENABLED=True)
    def test_assign_anyway_already_assigned_rejected(self) -> None:
        self._make_pr(101, assignees=["bob"])
        proposal = self._proposal(101, state=AssignmentProposal.STATE_ACCEPTED)
        self._login_session()

        with patch("console.views.assign_reviewer_and_record") as mock_assign:
            resp = self.client.post(reverse("console:assign-anyway", args=[proposal.id]))

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "already assigned")
        mock_assign.assert_not_called()

    @override_settings(ANALYZER_ASSIGNMENT_PROPOSALS_ASSIGN_ON_ACCEPT_ENABLED=True)
    def test_assign_anyway_other_reviewer_forbidden(self) -> None:
        self._make_pr(101)
        proposal = self._proposal(101, login="carol", state=AssignmentProposal.STATE_EXPIRED)
        self._login_session()  # signed in as bob

        resp = self.client.post(reverse("console:assign-anyway", args=[proposal.id]))
        self.assertEqual(resp.status_code, 403)

    # ---- unassign ------------------------------------------------------

    @override_settings(ANALYZER_ASSIGNMENT_PROPOSALS_CONSOLE_UNASSIGN_ENABLED=True)
    def test_unassign_removes_selected_self_only(self) -> None:
        self._make_pr(200, assignees=["bob"])
        self._make_pr(201, assignees=["bob"])
        self._login_session()
        fake_client = MagicMock()
        fake_client.unassign.return_value = ()  # bob is no longer in the resulting assignee set

        with (
            patch("core.services.github_operation_tokens.resolve_github_app_operation_token", return_value="tok"),
            patch("console.views.GitHubAssignmentClient", return_value=fake_client),
            patch("console.views._enqueue_pr_sync") as mock_sync,
        ):
            resp = self.client.post(reverse("console:unassign"), {"repo_id": self.repo.id, "pr_numbers": ["200", "201"]})

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Removed you from")
        self.assertContains(resp, "#200")
        self.assertContains(resp, "#201")
        # Each removed PR links out to its GitHub page.
        self.assertContains(resp, "https://github.com/leanprover-community/mathlib4/pull/200")
        self.assertEqual(mock_sync.call_count, 2)
        # Self-service only: the login unassigned is always the signed-in reviewer's, never the request's.
        for call in fake_client.unassign.call_args_list:
            self.assertEqual(call.kwargs["github_login"], "bob")

    def test_unassign_disabled_by_default(self) -> None:
        self._make_pr(200, assignees=["bob"])
        self._login_session()
        resp = self.client.post(reverse("console:unassign"), {"repo_id": self.repo.id, "pr_numbers": ["200"]})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "enabled yet")

    @override_settings(ANALYZER_ASSIGNMENT_PROPOSALS_CONSOLE_UNASSIGN_ENABLED=True)
    def test_unassign_reports_partial_failure(self) -> None:
        self._make_pr(200, assignees=["bob"])
        self._make_pr(201, assignees=["bob"])
        self._login_session()
        fake_client = MagicMock()

        def _unassign(*, owner, repo, number, github_login):
            if number == 201:
                raise AssignmentMutationError("github_error", "boom")
            return ()

        fake_client.unassign.side_effect = _unassign

        with (
            patch("core.services.github_operation_tokens.resolve_github_app_operation_token", return_value="tok"),
            patch("console.views.GitHubAssignmentClient", return_value=fake_client),
            patch("console.views._enqueue_pr_sync"),
        ):
            resp = self.client.post(reverse("console:unassign"), {"repo_id": self.repo.id, "pr_numbers": ["200", "201"]})

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Removed you from")
        self.assertContains(resp, "Could not unassign")

    @override_settings(ANALYZER_ASSIGNMENT_PROPOSALS_CONSOLE_UNASSIGN_ENABLED=True)
    def test_unassign_no_selection_redirects_home(self) -> None:
        self._login_session()
        resp = self.client.post(reverse("console:unassign"), {"repo_id": self.repo.id})
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("console:home"), resp["Location"])

    def test_unassign_without_session_redirects_login(self) -> None:
        resp = self.client.post(reverse("console:unassign"), {"repo_id": self.repo.id, "pr_numbers": ["200"]})
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("console:login"), resp["Location"])

    @override_settings(ANALYZER_ASSIGNMENT_PROPOSALS_CONSOLE_UNASSIGN_ENABLED=True)
    def test_home_shows_per_pr_load_contribution(self) -> None:
        # An AwaitingReview assigned PR contributes weight 1.0; the console surfaces "+1" per row and
        # the roster becomes an unassign form when the flag is on.
        self._make_pr(200, assignees=["bob"])
        self._seed_snapshot(prs={"200": {"assignees": ["bob"], "author": "alice", "pr_status": "AwaitingReview"}})
        self._login_session()

        resp = self.client.get(reverse("console:home"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Assigned to you (1)")
        self.assertContains(resp, "+1")
        self.assertContains(resp, "Unassign selected")
