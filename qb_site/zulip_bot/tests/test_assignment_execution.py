from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import Repository, ReviewerPreference, User
from syncer.models import PullRequest, PullRequestState
from zulip_bot.commands import CommandContext
from zulip_bot.services.assignment_execution import run_assignment_command


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
        with patch.dict("os.environ", {"GITHUB_ASSIGNMENT_TOKEN": "", "GH_TOKEN": "", "GITHUB_TOKEN": ""}, clear=False):
            result = run_assignment_command(
                action="assign",
                context=self._context(),
                args="https://github.com/leanprover-community/mathlib4/pull/1",
            )

        self.assertIn("GitHub assignment token is not configured", result.content)

    @override_settings(ZULIP_ASSIGNMENT_MUTATIONS_ENABLED="true", GITHUB_ASSIGNMENT_TOKEN="tok")
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
            patch("zulip_bot.services.assignment_execution.ZulipClient") as mock_zulip_client,
        ):
            mock_zulip_client.return_value.add_reaction.return_value = {"result": "success"}
            result = run_assignment_command(
                action="assign",
                context=self._context(),
                args="https://github.com/leanprover-community/mathlib4/pull/1",
            )

        self.assertTrue(result.response_not_required)
        self.assertEqual(result.content, "")
        mock_assign.assert_called_once()
        mock_zulip_client.return_value.add_reaction.assert_called_once()

    @override_settings(ZULIP_ASSIGNMENT_MUTATIONS_ENABLED="true", GITHUB_ASSIGNMENT_TOKEN="tok")
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
