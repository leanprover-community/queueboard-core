from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import Repository, ReviewerPreference, User
from syncer.models import PullRequest, PullRequestState
from zulip_bot.webhook.membership import GroupMembershipCheckError
from zulip_bot.tests.webhook_test_utils import WebhookTestMixin


@override_settings(
    ZULIP_WEBHOOK_TOKEN="test-token",
    ZULIP_BOT_EMAIL="qb-bot@example.com",
)
class TestZulipWebhookEndpoint(WebhookTestMixin, TestCase):
    def assert_ignored(self, result: dict) -> None:
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["json"], {"response_not_required": True})

    def test_method_not_allowed(self) -> None:
        response = self.client.get(reverse("zulip-webhook"))
        self.assertEqual(response.status_code, 405)

    def test_rejects_invalid_token(self) -> None:
        result = self._post_payload(self._payload(content="help", token="wrong", message_type="private"))
        self.assertEqual(result["status"], 403)

    @override_settings(
        ZULIP_COMMAND_POLICY={
            "help": {"allowed_groups": ["all"], "allowed_contexts": ["stream:5"]},
            "echo": {"allowed_groups": ["all"], "allowed_contexts": ["stream:5"]},
        }
    )
    def test_unknown_command_returns_help(self) -> None:
        result = self._post_payload(self._payload(content="@**qb-bot** unknown", id=12, stream_id=5))
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["json"]["type"], "private")
        self.assertIn("Unknown command", result["json"]["content"])
        self.assertIn("- help: List supported commands.", result["json"]["content"])

    @override_settings(
        ZULIP_COMMAND_POLICY={
            "help": {"allowed_groups": ["all"], "allowed_contexts": ["dm"]},
            "echo": {"allowed_groups": ["all"], "allowed_contexts": ["dm"]},
        }
    )
    def test_unknown_command_ignored_when_no_commands_allowed(self) -> None:
        result = self._post_payload(self._payload(content="@**qb-bot** unknown", id=14, stream_id=8))
        self.assert_ignored(result)

    @override_settings(
        ZULIP_COMMAND_POLICY={
            "echo": {"allowed_groups": ["all"], "allowed_contexts": ["all"]},
        }
    )
    def test_bot_sender_is_ignored(self) -> None:
        self.mock_is_bot_sender.return_value = True
        result = self._post_payload(self._payload(content="@**qb-bot** echo hello world", id=20, stream_id=5, sender_id=42))
        self.assert_ignored(result)

    @override_settings(
        ZULIP_COMMAND_POLICY={
            "help": {"allowed_groups": ["all"], "allowed_contexts": ["stream:5"]},
        }
    )
    def test_stream_message_with_nonleading_mention_is_ignored(self) -> None:
        result = self._post_payload(self._payload(content="Announcing @**qb-bot**: help", id=21, stream_id=5))
        self.assert_ignored(result)

    def test_invalid_json_payload_returns_400(self) -> None:
        response = self.client.post(
            reverse("zulip-webhook"),
            data="{",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload["error"], "Invalid payload")
        self.assertIn("invalid_json", payload["errors"][0])

    @override_settings(
        ZULIP_COMMAND_POLICY={
            "echo": {"allowed_groups": ["all"], "allowed_contexts": ["stream:5"]},
        }
    )
    def test_stream_command_replies_in_stream(self) -> None:
        result = self._post_payload(self._payload(content="@**qb-bot** echo hello", id=50, stream_id=5))
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["json"]["type"], "private")  # echo is PRIVATE

    @override_settings(
        ZULIP_COMMAND_POLICY={
            "echo": {"allowed_groups": ["all"], "allowed_contexts": ["all"]},
        }
    )
    def test_stream_registered_command_invoked_from_dm_replies_privately(self) -> None:
        """A command registered as STREAM should still reply privately when invoked via DM."""
        from zulip_bot.commands import CommandContext, CommandResult, ResponseMode, register_command

        # Register a temporary STREAM command for this test.
        @register_command(name="test-stream-cmd", description="test", response_mode=ResponseMode.STREAM)
        def _test_cmd(ctx: CommandContext, args: str) -> CommandResult:
            return CommandResult(content="stream-reply", response_mode=ResponseMode.STREAM)

        try:
            with override_settings(
                ZULIP_COMMAND_POLICY={
                    "test-stream-cmd": {"allowed_groups": ["all"], "allowed_contexts": ["all"]},
                }
            ):
                result = self._post_payload(
                    self._payload(content="test-stream-cmd", message_type="private")
                )
            self.assertEqual(result["status"], 200)
            self.assertEqual(result["json"]["type"], "private")
            self.assertEqual(result["json"]["content"], "stream-reply")
        finally:
            from zulip_bot.commands import _COMMANDS  # type: ignore[attr-defined]
            _COMMANDS.pop("test-stream-cmd", None)

    @override_settings(
        ZULIP_COMMAND_POLICY={
            "help": {"allowed_groups": [1234], "allowed_contexts": ["dm"]},
        }
    )
    def test_membership_failure_returns_private_structured_error(self) -> None:
        with (
            patch(
                "zulip_bot.views.allowed_command_names",
                side_effect=GroupMembershipCheckError(
                    "Zulip group membership check failed",
                    payload={"group_id": 1234, "zulip_error": {"msg": "This endpoint does not accept bot requests."}},
                ),
            ),
            patch("zulip_bot.views.logger.exception"),
        ):
            result = self._post_payload(self._payload(content="help", id=99, message_type="private"))

        self.assertEqual(result["status"], 200)
        self.assertEqual(result["json"]["type"], "private")
        self.assertIn("An unexpected error occurred", result["json"]["content"])
        self.assertIn('"error_type": "GroupMembershipCheckError"', result["json"]["content"])
        self.assertIn("does not accept bot requests", result["json"]["content"])

    @override_settings(
        ZULIP_COMMAND_POLICY={
            "help": {"allowed_groups": ["all"], "allowed_contexts": ["dm"]},
        }
    )
    def test_unexpected_error_returns_private_detailed_error(self) -> None:
        with (
            patch("zulip_bot.views.allowed_command_names", side_effect=RuntimeError("boom")),
            patch("zulip_bot.views.logger.exception"),
        ):
            result = self._post_payload(self._payload(content="help", id=100, message_type="private"))

        self.assertEqual(result["status"], 200)
        self.assertEqual(result["json"]["type"], "private")
        self.assertIn("An unexpected error occurred", result["json"]["content"])
        self.assertIn("```json", result["json"]["content"])
        self.assertIn('"error_type": "RuntimeError"', result["json"]["content"])

    @override_settings(
        ZULIP_COMMAND_POLICY={
            "echo": {"allowed_groups": ["all"], "allowed_contexts": ["dm"]},
        }
    )
    def test_command_handler_exception_returns_private_detailed_error(self) -> None:
        bad_command = SimpleNamespace(name="echo", handler=lambda *_: (_ for _ in ()).throw(RuntimeError("handler exploded")))
        with (
            patch("zulip_bot.views.get_command", return_value=bad_command),
            patch("zulip_bot.views.logger.exception"),
        ):
            result = self._post_payload(self._payload(content="echo hi", id=101, message_type="private"))

        self.assertEqual(result["status"], 200)
        self.assertEqual(result["json"]["type"], "private")
        self.assertIn('"message": "handler exploded"', result["json"]["content"])
        self.assertIn("```json", result["json"]["content"])

    @override_settings(
        ZULIP_COMMAND_POLICY={
            "assign": {"allowed_groups": ["all"], "allowed_contexts": ["stream:5"]},
        }
    )
    def test_assign_command_executes_and_returns_private_preflight_summary(self) -> None:
        repo = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        user = User.objects.create(zulip_user_id=101, github_login="reviewer")
        ReviewerPreference.objects.create(repository=repo, user=user)

        result = self._post_payload(
            self._payload(
                content="@**qb-bot** assign https://github.com/leanprover-community/mathlib4/pull/123",
                id=102,
                stream_id=5,
            )
        )

        self.assertEqual(result["status"], 200)
        self.assertEqual(result["json"]["type"], "private")
        self.assertIn("Preflight passed for `assign` on leanprover-community/mathlib4#123.", result["json"]["content"])

    @override_settings(
        ZULIP_COMMAND_POLICY={
            "assign": {"allowed_groups": ["all"], "allowed_contexts": ["stream:5"]},
        },
        ZULIP_ASSIGNMENT_MUTATIONS_ENABLED="true",
    )
    def test_assign_command_clean_success_returns_private_summary_with_assignees(self) -> None:
        repo = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        user = User.objects.create(zulip_user_id=101, github_login="reviewer")
        ReviewerPreference.objects.create(repository=repo, user=user)
        now = timezone.now()
        PullRequest.objects.create(
            repository=repo,
            number=124,
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
            patch(
                "zulip_bot.services.assignment_execution.GitHubAssignmentClient.assign_many",
                return_value=("reviewer",),
            ),
            patch("core.services.github_operation_tokens.get_default_github_app_token_provider") as mock_provider,
        ):
            mock_provider.return_value.get_token.return_value = "app-token"
            result = self._post_payload(
                self._payload(
                    content="@**qb-bot** assign https://github.com/leanprover-community/mathlib4/pull/124",
                    id=103,
                    stream_id=5,
                )
            )

        self.assertEqual(result["status"], 200)
        self.assertEqual(result["json"]["type"], "private")
        self.assertIn("assign succeeded for `reviewer`.", result["json"]["content"])
        self.assertIn("Current assignees: `reviewer`.", result["json"]["content"])
