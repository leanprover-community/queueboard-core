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
    def test_command_reply_does_not_include_type_field(self) -> None:
        """Webhook responses must not include a 'type' field.

        Zulip's outgoing webhook spec does not use a 'type' field to redirect
        the reply destination — replies always go back to the triggering
        conversation. Commands that must send private content do so via
        ZulipClient.send_direct_message() and return response_not_required=True.
        """
        result = self._post_payload(self._payload(content="@**qb-bot** echo hello", id=50, stream_id=5))
        self.assertEqual(result["status"], 200)
        self.assertIn("hello", result["json"]["content"])
        self.assertNotIn("type", result["json"])

    @override_settings(
        ZULIP_COMMAND_POLICY={
            "echo": {"allowed_groups": ["all"], "allowed_contexts": ["dm"]},
        }
    )
    def test_dm_with_leading_bot_mention_strips_mention_and_executes(self) -> None:
        """A DM that starts with @**botname** should have the mention stripped before parsing."""
        result = self._post_payload(self._payload(content="@**qb-bot** echo hello", message_type="private"))
        self.assertEqual(result["status"], 200)
        self.assertIn("hello", result["json"]["content"])

    @override_settings(
        ZULIP_COMMAND_POLICY={
            "echo": {"allowed_groups": ["all"], "allowed_contexts": ["dm"]},
        }
    )
    def test_dm_without_mention_still_executes(self) -> None:
        """DMs without a leading mention continue to work as before."""
        result = self._post_payload(self._payload(content="echo hello", message_type="private"))
        self.assertEqual(result["status"], 200)
        self.assertIn("hello", result["json"]["content"])

    @override_settings(
        ZULIP_COMMAND_POLICY={
            "echo": {"allowed_groups": ["all"], "allowed_contexts": ["dm"]},
        }
    )
    def test_dm_with_non_leading_mention_is_not_stripped(self) -> None:
        """A mid-message mention of the bot in a DM is not stripped; the first word becomes the command."""
        result = self._post_payload(self._payload(content="echo @**qb-bot** hello", message_type="private"))
        self.assertEqual(result["status"], 200)
        # echo command receives "@**qb-bot** hello" as args; the mention is preserved
        self.assertIn("@**qb-bot** hello", result["json"]["content"])

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
        self.assertIn('"message": "handler exploded"', result["json"]["content"])
        self.assertIn("```json", result["json"]["content"])

    @override_settings(
        ZULIP_COMMAND_POLICY={
            "assign": {"allowed_groups": ["all"], "allowed_contexts": ["stream:5"]},
        }
    )
    def test_assign_command_executes_and_returns_preflight_summary(self) -> None:
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
        self.assertIn("Preflight passed for `assign` on leanprover-community/mathlib4#123.", result["json"]["content"])

    @override_settings(
        ZULIP_COMMAND_POLICY={
            "assign": {"allowed_groups": ["all"], "allowed_contexts": ["stream:5"]},
        },
        ZULIP_ASSIGNMENT_MUTATIONS_ENABLED="true",
    )
    def test_assign_command_clean_success_returns_summary_with_assignees(self) -> None:
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
        self.assertIn("assign succeeded for `reviewer`.", result["json"]["content"])
        self.assertIn("Current assignees: `reviewer`.", result["json"]["content"])
