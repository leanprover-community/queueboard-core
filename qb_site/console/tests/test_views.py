from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from analyzer.models import AssignmentProposal, ReviewerOptOut
from console.session import SESSION_NONCE_KEY, SESSION_USER_KEY
from core.models import Repository, ReviewerPreference, User
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
        self._login_session()

        resp = self.client.get(reverse("console:home"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "mathlib4 #101")
        self.assertContains(resp, "Accept")
        self.assertContains(resp, "Decline")

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

    def test_decline_already_terminal_renders_unavailable(self) -> None:
        self._make_pr(101)
        proposal = self._proposal(101, state=AssignmentProposal.STATE_ACCEPTED)
        self._login_session()

        resp = self.client.post(reverse("console:decline", args=[proposal.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "already been decided")
        self.assertFalse(ReviewerOptOut.objects.filter(repository=self.repo, pr_number=101).exists())
