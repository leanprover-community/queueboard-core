from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.test import TestCase, override_settings

from analyzer.models import PRQueueWindow, QueueRuleSet
from core.models import Repository, ReviewerPreference, User
from syncer.models import PullRequest, PRTimelineEvent, PRTimelineEventType
from zulip_bot.commands import CommandContext
from zulip_bot.commands.assigned_prs import assigned_prs_command, _split_message_chunks


def _dt(year: int, month: int, day: int, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=dt_timezone.utc)


@override_settings(
    ZULIP_BASE_URL="https://leanprover.zulipchat.com",
    ZULIP_BOT_EMAIL="qb-bot@example.com",
    ZULIP_BOT_API_KEY="bot-key",
)
class TestAssignedPrsCommand(TestCase):
    def _context(self, *, sender_id: int | None = 101) -> CommandContext:
        return CommandContext(
            sender_id=sender_id,
            sender_email="reviewer@example.com",
            sender_full_name="Reviewer User",
            message_content="assigned_prs",
            message_id=1,
            stream_id=None,
            topic=None,
            is_private=True,
            allowed_command_names=frozenset({"assigned_prs"}),
        )

    def test_returns_error_when_sender_missing(self) -> None:
        result = assigned_prs_command(self._context(sender_id=None), "")
        self.assertIn("Could not determine your Zulip identity", result.content)
        self.assertFalse(result.response_not_required)

    def test_returns_error_when_user_not_linked(self) -> None:
        result = assigned_prs_command(self._context(), "")
        self.assertIn("No reviewer profile is linked", result.content)
        self.assertFalse(result.response_not_required)

    def test_sends_human_readable_report(self) -> None:
        user = User.objects.create(github_login="alice", zulip_user_id=101)
        repo = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        ReviewerPreference.objects.create(
            user=user,
            repository=repo,
            notifications_enabled=True,
            notification_settings={"stale_nudge_days": 14, "auto_unassign_days": 21},
        )
        rules = QueueRuleSet.objects.create(
            repository=repo,
            version=1,
            require_open=True,
            require_not_draft=True,
            require_ci_success=False,
            required_label_names=[],
            forbidden_label_names=[],
            is_active=True,
        )
        now_ts = _dt(2026, 2, 23, 12)
        pr = PullRequest.objects.create(
            repository=repo,
            number=123,
            author=None,
            state="open",
            is_draft=False,
            gh_created_at=now_ts - timedelta(days=30),
            gh_updated_at=now_ts,
            base_ref_name="master",
            head_ref_name="branch",
            head_repo_owner_login="leanprover-community",
            head_repo_name="mathlib4",
            title="Improve queue windows",
            body="",
            additions=0,
            deletions=0,
            changed_files_count=0,
            assignees=["alice"],
            approvals=[],
            commenters=[],
            files=[],
        )
        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.ASSIGNED,
            occurred_at=now_ts - timedelta(days=16),
            assignee_login="alice",
        )
        PRQueueWindow.objects.create(
            pull_request=pr,
            rule_set=rules,
            from_ts=now_ts - timedelta(days=16),
            to_ts=None,
            cycle_index=0,
            duration_seconds_closed=0,
            cumulative_seconds_closed=0,
            window_count=1,
            first_on_queue_ts=now_ts - timedelta(days=16),
        )

        with patch("zulip_bot.commands.assigned_prs.ZulipClient.send_direct_message") as mock_send:
            result = assigned_prs_command(self._context(), "")

        self.assertTrue(result.response_not_required)
        self.assertEqual(mock_send.call_count, 1)
        kwargs = mock_send.call_args.kwargs
        self.assertEqual(kwargs["to"], [101])
        self.assertIn("Assigned PRs report for `alice`", kwargs["content"])
        self.assertIn("leanprover-community/mathlib4", kwargs["content"])
        self.assertIn("PR #123", kwargs["content"])
        self.assertIn("Consecutive queue age since assignment", kwargs["content"])
        self.assertIn("Total queue time", kwargs["content"])


class TestSplitMessageChunks(TestCase):
    def test_splits_when_message_exceeds_limit(self) -> None:
        content = "line-1\nline-2\nline-3\nline-4"
        chunks = _split_message_chunks(content=content, max_chars=12)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 12 for chunk in chunks))
