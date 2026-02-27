from __future__ import annotations
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.test import TestCase, override_settings

from analyzer.models import PRQueueWindow, QueueRuleSet
from analyzer.services.reviewer_attention_format import format_compact_duration
from core.models import Repository, ReviewerPreference, User
from syncer.models import LabelDef, PRLabel, PullRequest, PRTimelineEvent, PRTimelineEventType
from zulip_bot.commands import CommandContext
from zulip_bot.commands.assigned_prs import _split_message_chunks, assigned_prs_command


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
        self.assertIn("```spoiler On Queue (1)", kwargs["content"])
        self.assertIn("```spoiler Maintainer Merged (0)", kwargs["content"])
        self.assertIn("```spoiler Not On Queue (0)", kwargs["content"])
        self.assertIn("PR #123", kwargs["content"])
        self.assertIn("Assigned:", kwargs["content"])
        self.assertRegex(
            kwargs["content"],
            r"Consecutive time on queue since latest assignment: [0-9]+d(?: [0-9]+h)?",
        )
        self.assertRegex(kwargs["content"], r"Total queue time: [0-9]+d(?: [0-9]+h)?")
        self.assertNotIn("On queue now", kwargs["content"])
        self.assertNotIn("seconds)", kwargs["content"])

    def test_maintainer_merge_prs_are_separated_into_own_section(self) -> None:
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
        label = LabelDef.objects.create(repository=repo, name="maintainer-merge", color="123abc")
        now_ts = _dt(2026, 2, 23, 12)
        pr = PullRequest.objects.create(
            repository=repo,
            number=124,
            author=None,
            state="open",
            is_draft=False,
            gh_created_at=now_ts - timedelta(days=8),
            gh_updated_at=now_ts,
            base_ref_name="master",
            head_ref_name="branch2",
            head_repo_owner_login="leanprover-community",
            head_repo_name="mathlib4",
            title="Maintainer merged PR",
            body="",
            additions=0,
            deletions=0,
            changed_files_count=0,
            assignees=["alice"],
            approvals=[],
            commenters=[],
            files=[],
        )
        PRLabel.objects.create(pull_request=pr, label_def=label)
        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.ASSIGNED,
            occurred_at=now_ts - timedelta(days=8),
            assignee_login="alice",
        )
        PRQueueWindow.objects.create(
            pull_request=pr,
            rule_set=rules,
            from_ts=now_ts - timedelta(days=8),
            to_ts=None,
            cycle_index=0,
            duration_seconds_closed=0,
            cumulative_seconds_closed=0,
            window_count=1,
            first_on_queue_ts=now_ts - timedelta(days=8),
        )

        with patch("zulip_bot.commands.assigned_prs.ZulipClient.send_direct_message") as mock_send:
            result = assigned_prs_command(self._context(), "")

        self.assertTrue(result.response_not_required)
        kwargs = mock_send.call_args.kwargs
        self.assertIn("```spoiler On Queue (0)", kwargs["content"])
        self.assertIn("```spoiler Maintainer Merged (1)", kwargs["content"])
        self.assertIn("PR #124", kwargs["content"])


class TestSplitMessageChunks(TestCase):
    def test_splits_when_message_exceeds_limit(self) -> None:
        content = "line-1\nline-2\nline-3\nline-4"
        chunks = _split_message_chunks(content=content, max_chars=12)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 12 for chunk in chunks))


class TestFormatDuration(TestCase):
    def test_format_duration_thresholds(self) -> None:
        self.assertEqual(format_compact_duration(45), "45s")
        self.assertEqual(format_compact_duration(3 * 60 + 2), "3m 2s")
        self.assertEqual(format_compact_duration(2 * 3600 + 5 * 60), "2h 5m")
        self.assertEqual(format_compact_duration(2 * 86400 + 4 * 3600), "2d 4h")
        self.assertEqual(format_compact_duration(8 * 86400 + 17 * 3600), "8d")
