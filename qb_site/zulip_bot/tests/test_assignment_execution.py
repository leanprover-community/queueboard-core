from __future__ import annotations

from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import Repository, ReviewerPreference, User
from syncer.models import PullRequest, PullRequestState
from zulip_bot.commands import CommandContext
from zulip_bot.services.assignment_execution import LivePullRequestView, run_assignment_command


class TestAssignmentExecution(TestCase):
    def _context(self, *, sender_id: int | None = 101) -> CommandContext:
        return CommandContext(
            sender_id=sender_id,
            sender_email="reviewer@example.com",
            sender_full_name="Reviewer User",
            message_content="assign",
            message_id=555,
            stream_id=None,
            topic=None,
            is_private=True,
            rendered_content=None,
            allowed_command_names=frozenset({"assign", "unassign"}),
        )

    def _make_repo_user_pref(self) -> tuple[Repository, User]:
        repo = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        user = User.objects.create(zulip_user_id=101, github_login="reviewer")
        ReviewerPreference.objects.create(repository=repo, user=user)
        return repo, user

    def test_returns_preflight_summary_when_mutations_disabled(self) -> None:
        self._make_repo_user_pref()

        result = run_assignment_command(
            action="assign",
            context=self._context(),
            args="https://github.com/leanprover-community/mathlib4/pull/1",
        )

        self.assertIn("Summary for `assign`", result.content)
        self.assertIn("GitHub assignment mutation is disabled", result.content)

    @override_settings(ZULIP_ASSIGNMENT_MUTATIONS_ENABLED="true")
    def test_reports_token_missing_when_enabled_without_token(self) -> None:
        self._make_repo_user_pref()
        result = run_assignment_command(
            action="assign",
            context=self._context(),
            args="https://github.com/leanprover-community/mathlib4/pull/1",
        )

        self.assertIn("GitHub App token for assignment is not available", result.content)

    @override_settings(ZULIP_ASSIGNMENT_MUTATIONS_ENABLED="true")
    def test_returns_response_not_required_on_clean_success(self) -> None:
        repo, user = self._make_repo_user_pref()
        now = timezone.now()
        PullRequest.objects.create(
            repository=repo,
            number=1,
            author=user,
            state=PullRequestState.OPEN,
            is_draft=False,
            gh_created_at=now,
            gh_updated_at=now,
            base_ref_name="master",
            head_ref_name="branch",
            head_repo_owner_login="leanprover-community",
            head_repo_name="mathlib4",
            title="t",
            body="b",
            additions=1,
            deletions=0,
            changed_files_count=1,
            assignees=[],
        )

        with (
            patch("zulip_bot.services.assignment_execution.GitHubAssignmentClient.assign", return_value=None) as mock_assign,
            patch("core.services.github_operation_tokens.get_default_github_app_token_provider") as mock_provider,
            patch("zulip_bot.services.assignment_execution.ZulipClient") as mock_zulip_client,
            patch("zulip_bot.services.assignment_execution._enqueue_post_action_sync") as mock_enqueue_sync,
        ):
            mock_provider.return_value.get_token.return_value = "app-token"
            mock_zulip_client.return_value.add_reaction.return_value = {"result": "success"}
            result = run_assignment_command(
                action="assign",
                context=self._context(),
                args="https://github.com/leanprover-community/mathlib4/pull/1",
            )

        self.assertTrue(result.response_not_required)
        self.assertEqual(result.content, "")
        mock_assign.assert_called_once()
        mock_enqueue_sync.assert_called_once()
        mock_zulip_client.return_value.add_reaction.assert_called_once()

    @override_settings(ZULIP_ASSIGNMENT_MUTATIONS_ENABLED="true")
    def test_unassign_local_not_assigned_yields_warning_and_no_mutation(self) -> None:
        repo, user = self._make_repo_user_pref()
        now = timezone.now()
        PullRequest.objects.create(
            repository=repo,
            number=2,
            author=user,
            state=PullRequestState.OPEN,
            is_draft=False,
            gh_created_at=now,
            gh_updated_at=now,
            base_ref_name="master",
            head_ref_name="branch",
            head_repo_owner_login="leanprover-community",
            head_repo_name="mathlib4",
            title="t",
            body="b",
            additions=1,
            deletions=0,
            changed_files_count=1,
            assignees=["other"],
        )

        with patch("zulip_bot.services.assignment_execution.GitHubAssignmentClient.unassign", return_value=None) as mock_unassign:
            result = run_assignment_command(
                action="unassign",
                context=self._context(),
                args="https://github.com/leanprover-community/mathlib4/pull/2",
            )

        self.assertIn("is not currently assigned", result.content)
        self.assertIn("No valid reviewers to unassign", result.content)
        mock_unassign.assert_not_called()

    @override_settings(ZULIP_ASSIGNMENT_MUTATIONS_ENABLED="true")
    def test_missing_local_pr_uses_live_fallback_closed_pr(self) -> None:
        self._make_repo_user_pref()
        with patch(
            "zulip_bot.services.assignment_execution._fetch_live_pr_view",
            return_value=LivePullRequestView(is_open=False, assignees_lc=frozenset()),
        ):
            result = run_assignment_command(
                action="assign",
                context=self._context(),
                args="https://github.com/leanprover-community/mathlib4/pull/3",
            )

        self.assertIn("Pull request is not open in GitHub live data.", result.content)
        self.assertIn("No valid reviewers to assign after validation.", result.content)

    @override_settings(ZULIP_ASSIGNMENT_MUTATIONS_ENABLED="true")
    def test_missing_local_pr_uses_live_fallback_for_unassign_idempotency(self) -> None:
        self._make_repo_user_pref()
        with patch(
            "zulip_bot.services.assignment_execution._fetch_live_pr_view",
            return_value=LivePullRequestView(is_open=True, assignees_lc=frozenset({"someone-else"})),
        ):
            result = run_assignment_command(
                action="unassign",
                context=self._context(),
                args="https://github.com/leanprover-community/mathlib4/pull/4",
            )

        self.assertIn("is not currently assigned (github live data).", result.content)
        self.assertIn("No valid reviewers to unassign after validation.", result.content)

    @override_settings(ZULIP_ASSIGNMENT_MUTATIONS_ENABLED="true")
    def test_assign_uses_github_app_token_provider_when_available(self) -> None:
        repo, user = self._make_repo_user_pref()
        now = timezone.now()
        PullRequest.objects.create(
            repository=repo,
            number=5,
            author=user,
            state=PullRequestState.OPEN,
            is_draft=False,
            gh_created_at=now,
            gh_updated_at=now,
            base_ref_name="master",
            head_ref_name="branch",
            head_repo_owner_login="leanprover-community",
            head_repo_name="mathlib4",
            title="t",
            body="b",
            additions=1,
            deletions=0,
            changed_files_count=1,
            assignees=[],
        )

        provider = Mock()
        provider.get_token.return_value = "app-token"
        with (
            patch("core.services.github_operation_tokens.get_default_github_app_token_provider", return_value=provider),
            patch("zulip_bot.services.assignment_execution.requests.request") as mock_request,
            patch("zulip_bot.services.assignment_execution.ZulipClient") as mock_zulip_client,
            patch("zulip_bot.services.assignment_execution._enqueue_post_action_sync"),
        ):
            response = Mock()
            response.status_code = 200
            response.json.return_value = {}
            mock_request.return_value = response
            mock_zulip_client.return_value.add_reaction.return_value = {"result": "success"}
            result = run_assignment_command(
                action="assign",
                context=self._context(),
                args="https://github.com/leanprover-community/mathlib4/pull/5",
            )

        self.assertTrue(result.response_not_required)
        provider.get_token.assert_called_once_with(
            operation="assign_pr",
            owner="leanprover-community",
            repo="mathlib4",
        )
        self.assertEqual(mock_request.call_args.kwargs["headers"]["Authorization"], "Bearer app-token")

    @override_settings(ZULIP_ASSIGNMENT_MUTATIONS_ENABLED="true")
    def test_unassign_fails_when_app_token_missing(self) -> None:
        repo, user = self._make_repo_user_pref()
        now = timezone.now()
        PullRequest.objects.create(
            repository=repo,
            number=7,
            author=user,
            state=PullRequestState.OPEN,
            is_draft=False,
            gh_created_at=now,
            gh_updated_at=now,
            base_ref_name="master",
            head_ref_name="branch",
            head_repo_owner_login="leanprover-community",
            head_repo_name="mathlib4",
            title="t",
            body="b",
            additions=1,
            deletions=0,
            changed_files_count=1,
            assignees=["reviewer"],
        )

        provider = Mock()
        provider.get_token.return_value = None
        with (
            patch("core.services.github_operation_tokens.get_default_github_app_token_provider", return_value=provider),
            patch("zulip_bot.services.assignment_execution.requests.request") as mock_request,
        ):
            result = run_assignment_command(
                action="unassign",
                context=self._context(),
                args="https://github.com/leanprover-community/mathlib4/pull/7",
            )

        self.assertIn("GitHub App token for assignment is not available", result.content)
        mock_request.assert_not_called()
