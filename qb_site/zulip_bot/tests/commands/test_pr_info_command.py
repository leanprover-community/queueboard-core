from __future__ import annotations

import dataclasses
from datetime import datetime, timezone as dt_timezone
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from analyzer.services.pr_info import DependencyInfo, PRQueueInfo
from core.models import User
from zulip_bot.commands import CommandContext
from zulip_bot.commands.pr_info import (
    _build_mention_map,
    _format_pr_info,
    _parse_pr_refs,
    pr_info_command,
)
from zulip_bot.services.zulip_client import ZulipApiError


def _dt(year: int, month: int, day: int, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=dt_timezone.utc)


def _context(
    *,
    message_id: int = 999,
    stream_id: int | None = 5,
    topic: str | None = "PR review",
    is_private: bool = False,
    rendered_content: str | None = None,
    message_content: str = "pr-info",
) -> CommandContext:
    return CommandContext(
        sender_id=101,
        sender_email="user@example.com",
        sender_full_name="Test User",
        message_content=message_content,
        message_id=message_id,
        stream_id=stream_id,
        topic=topic,
        is_private=is_private,
        rendered_content=rendered_content,
        allowed_command_names=frozenset({"pr-info"}),
    )


def _make_pr_info(
    *,
    number: int = 123,
    title: str = "Fix something",
    on_queue: bool = True,
    state: str = "open",
    is_draft: bool = False,
    author_login: str | None = "alice",
    labels: list[str] | None = None,
    assignee_logins: list[str] | None = None,
    ci_status: str = "pass",
    off_queue_reasons: list[str] | None = None,
    total_queue_seconds: int | None = 86400,
    queue_since: datetime | None = None,
    snapshot_generated_at: datetime | None = None,
    snapshot_is_stale: bool = False,
) -> PRQueueInfo:
    return PRQueueInfo(
        owner="leanprover-community",
        repo="mathlib4",
        number=number,
        title=title,
        url=f"https://github.com/leanprover-community/mathlib4/pull/{number}",
        state=state,
        is_draft=is_draft,
        author_login=author_login,
        created_at=_dt(2026, 1, 1),
        updated_at=_dt(2026, 3, 1),
        closed_at=None,
        merged_at=None,
        labels=labels or ["awaiting-review"],
        assignee_logins=assignee_logins or [],
        ci_status=ci_status,
        on_queue=on_queue,
        off_queue_reasons=off_queue_reasons or [],
        queue_since=queue_since or _dt(2026, 2, 1),
        total_queue_seconds=total_queue_seconds,
        dependencies=[],
        snapshot_generated_at=snapshot_generated_at or _dt(2026, 3, 1, 12),
        snapshot_is_stale=snapshot_is_stale,
        source="snapshot",
    )


# ---------------------------------------------------------------------------
# Link parsing tests (no DB required)
# ---------------------------------------------------------------------------


class ParsePrRefsTests(TestCase):
    def test_href_from_rendered_content(self) -> None:
        html = (
            '<p>See <a href="https://github.com/leanprover-community/mathlib4/pull/1234">'
            "https://github.com/leanprover-community/mathlib4/pull/1234</a></p>"
        )
        ctx = _context(rendered_content=html, message_content="pr-info")
        refs = _parse_pr_refs(ctx, "")
        self.assertEqual(refs, [("leanprover-community", "mathlib4", 1234)])

    def test_multiple_hrefs_from_rendered_content(self) -> None:
        html = '<a href="https://github.com/org/repo/pull/10">PR 10</a> <a href="https://github.com/org/repo/pull/20">PR 20</a>'
        ctx = _context(rendered_content=html)
        refs = _parse_pr_refs(ctx, "")
        self.assertEqual(len(refs), 2)
        self.assertIn(("org", "repo", 10), refs)
        self.assertIn(("org", "repo", 20), refs)

    def test_deduplication(self) -> None:
        html = (
            '<a href="https://github.com/org/repo/pull/99">link</a> <a href="https://github.com/org/repo/pull/99">same link</a>'
        )
        ctx = _context(rendered_content=html)
        refs = _parse_pr_refs(ctx, "")
        self.assertEqual(len(refs), 1)

    def test_cap_at_ten(self) -> None:
        links = " ".join(f'<a href="https://github.com/o/r/pull/{i}">PR {i}</a>' for i in range(1, 15))
        ctx = _context(rendered_content=links)
        refs = _parse_pr_refs(ctx, "")
        self.assertEqual(len(refs), 10)

    def test_fallback_to_plain_url_in_args(self) -> None:
        ctx = _context(rendered_content=None)
        refs = _parse_pr_refs(ctx, "https://github.com/leanprover-community/mathlib4/pull/42")
        self.assertEqual(refs, [("leanprover-community", "mathlib4", 42)])

    def test_empty_returns_empty(self) -> None:
        ctx = _context(rendered_content=None)
        refs = _parse_pr_refs(ctx, "no links here")
        self.assertEqual(refs, [])

    def test_rendered_content_preferred_over_args(self) -> None:
        # rendered_content has PR 10, args has PR 20; should use rendered_content
        html = '<a href="https://github.com/o/r/pull/10">PR 10</a>'
        ctx = _context(rendered_content=html)
        refs = _parse_pr_refs(ctx, "https://github.com/o/r/pull/20")
        self.assertEqual(refs, [("o", "r", 10)])


# ---------------------------------------------------------------------------
# Mention map tests
# ---------------------------------------------------------------------------


class BuildMentionMapTests(TestCase):
    def test_known_user_gets_silent_mention(self) -> None:
        User.objects.create(github_login="alice", zulip_user_id=123, zulip_full_name="Alice Smith")
        result = _build_mention_map({"alice"})
        self.assertEqual(result["alice"], "@_**Alice Smith|123**")

    def test_unknown_user_absent_from_map(self) -> None:
        result = _build_mention_map({"ghost"})
        self.assertNotIn("ghost", result)

    def test_user_without_zulip_id_absent_from_map(self) -> None:
        User.objects.create(github_login="bob", zulip_user_id=None)
        result = _build_mention_map({"bob"})
        self.assertNotIn("bob", result)

    def test_empty_logins_returns_empty(self) -> None:
        result = _build_mention_map(set())
        self.assertEqual(result, {})


# ---------------------------------------------------------------------------
# Message formatting tests (pure logic, no DB or Zulip needed)
# ---------------------------------------------------------------------------


class FormatPrInfoTests(TestCase):
    def _now(self) -> datetime:
        return _dt(2026, 3, 1, 12)

    def test_on_queue_shows_queue_status(self) -> None:
        info = _make_pr_info(on_queue=True, queue_since=_dt(2026, 2, 1), total_queue_seconds=28 * 86400)
        text = _format_pr_info(info, {}, self._now())
        self.assertIn("**On queue**", text)
        self.assertIn("Total queue time", text)

    def test_not_on_queue_shows_reasons(self) -> None:
        info = _make_pr_info(on_queue=False, off_queue_reasons=["awaiting author", "labeled WIP"])
        text = _format_pr_info(info, {}, self._now())
        self.assertIn("**Not on queue**", text)
        self.assertIn("awaiting author", text)
        self.assertIn("labeled WIP", text)

    def test_merged_shows_state_tag(self) -> None:
        info = _make_pr_info(state="merged", on_queue=False, off_queue_reasons=[])
        text = _format_pr_info(info, {}, self._now())
        self.assertIn("[merged]", text)

    def test_draft_shows_draft_tag(self) -> None:
        info = _make_pr_info(is_draft=True, on_queue=False, off_queue_reasons=["draft PR"])
        text = _format_pr_info(info, {}, self._now())
        self.assertIn("[draft]", text)

    def test_pr_link_formatted_correctly(self) -> None:
        info = _make_pr_info(number=1234)
        text = _format_pr_info(info, {}, self._now())
        self.assertIn("[leanprover-community/mathlib4#1234]", text)
        self.assertIn("https://github.com/leanprover-community/mathlib4/pull/1234", text)

    def test_known_author_gets_silent_mention(self) -> None:
        info = _make_pr_info(author_login="alice")
        mention_map = {"alice": "@_**Alice Smith|123**"}
        text = _format_pr_info(info, mention_map, self._now())
        self.assertIn("@_**Alice Smith|123**", text)

    def test_unknown_author_gets_backtick(self) -> None:
        info = _make_pr_info(author_login="ghost")
        text = _format_pr_info(info, {}, self._now())
        self.assertIn("`ghost`", text)

    def test_stale_snapshot_shows_warning(self) -> None:
        info = _make_pr_info(snapshot_is_stale=True)
        text = _format_pr_info(info, {}, self._now())
        self.assertIn("⚠️ stale", text)

    def test_labels_rendered(self) -> None:
        info = _make_pr_info(labels=["t-algebra", "awaiting-review"])
        text = _format_pr_info(info, {}, self._now())
        self.assertIn("`t-algebra`", text)
        self.assertIn("`awaiting-review`", text)

    def test_dependency_link_included(self) -> None:
        dep = DependencyInfo(
            owner="leanprover-community",
            repo="mathlib4",
            number=100,
            state="merged",
            is_draft=False,
            title="Dep PR",
        )
        base_info = _make_pr_info()
        info = dataclasses.replace(base_info, dependencies=[dep])
        text = _format_pr_info(info, {}, self._now())
        self.assertIn("[leanprover-community/mathlib4#100]", text)
        self.assertIn("[merged]", text)


# ---------------------------------------------------------------------------
# Full command flow tests (mocked service + Zulip client)
# ---------------------------------------------------------------------------


@override_settings(
    ZULIP_BASE_URL="https://leanprover.zulipchat.com",
    ZULIP_BOT_EMAIL="qb-bot@example.com",
    ZULIP_BOT_API_KEY="bot-key",
)
class PrInfoCommandTests(TestCase):
    def test_no_links_returns_private_error(self) -> None:
        ctx = _context(rendered_content=None)
        result = pr_info_command(ctx, "no links here")
        self.assertFalse(result.response_not_required)
        self.assertIn("No GitHub PR links found", result.content)

    @patch("zulip_bot.commands.pr_info.get_pr_queue_info")
    @patch("zulip_bot.commands.pr_info.ZulipClient")
    def test_sends_stream_message_for_each_pr(self, MockZulipClient: MagicMock, mock_get_info: MagicMock) -> None:
        mock_client = MockZulipClient.return_value
        mock_get_info.return_value = _make_pr_info()
        html = (
            '<a href="https://github.com/leanprover-community/mathlib4/pull/1">PR 1</a> '
            '<a href="https://github.com/leanprover-community/mathlib4/pull/2">PR 2</a>'
        )
        ctx = _context(rendered_content=html, stream_id=5, topic="review")

        result = pr_info_command(ctx, "")

        self.assertTrue(result.response_not_required)
        mock_client.add_reaction.assert_called_once_with(message_id=999, emoji_name="eyes")
        self.assertEqual(mock_client.send_stream_message.call_count, 2)
        for call in mock_client.send_stream_message.call_args_list:
            self.assertEqual(call.kwargs["stream"], 5)
            self.assertEqual(call.kwargs["topic"], "review")

    @patch("zulip_bot.commands.pr_info.get_pr_queue_info")
    @patch("zulip_bot.commands.pr_info.ZulipClient")
    def test_pr_not_found_sends_not_found_message(self, MockZulipClient: MagicMock, mock_get_info: MagicMock) -> None:
        mock_client = MockZulipClient.return_value
        mock_get_info.return_value = None
        html = '<a href="https://github.com/org/repo/pull/999">PR 999</a>'
        ctx = _context(rendered_content=html)

        pr_info_command(ctx, "")

        mock_client.send_stream_message.assert_called_once()
        content = mock_client.send_stream_message.call_args.kwargs["content"]
        self.assertIn("not found", content)

    @patch("zulip_bot.commands.pr_info.get_pr_queue_info")
    @patch("zulip_bot.commands.pr_info.ZulipClient")
    def test_reaction_failure_does_not_abort(self, MockZulipClient: MagicMock, mock_get_info: MagicMock) -> None:
        mock_client = MockZulipClient.return_value
        mock_client.add_reaction.side_effect = ZulipApiError("reaction failed")
        mock_get_info.return_value = _make_pr_info()
        html = '<a href="https://github.com/org/repo/pull/1">PR 1</a>'
        ctx = _context(rendered_content=html)

        result = pr_info_command(ctx, "")

        # Despite the reaction failure, message should still be sent
        self.assertTrue(result.response_not_required)
        mock_client.send_stream_message.assert_called_once()

    @patch("zulip_bot.commands.pr_info.get_pr_queue_info")
    @patch("zulip_bot.commands.pr_info.ZulipClient")
    def test_dm_context_uses_direct_message(self, MockZulipClient: MagicMock, mock_get_info: MagicMock) -> None:
        mock_client = MockZulipClient.return_value
        mock_get_info.return_value = _make_pr_info()
        html = '<a href="https://github.com/org/repo/pull/1">PR 1</a>'
        ctx = _context(rendered_content=html, stream_id=None, topic=None, is_private=True)

        pr_info_command(ctx, "")

        mock_client.send_direct_message.assert_called_once()
        mock_client.send_stream_message.assert_not_called()
