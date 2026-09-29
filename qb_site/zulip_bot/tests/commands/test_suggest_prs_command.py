from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from django.utils import timezone

from analyzer.models import QueueSnapshot
from core.models import Repository, ReviewerPreference, User
from syncer.models import LabelDef
from zulip_bot.commands import CommandContext, CommandResult, get_command
from zulip_bot.commands.suggest_prs import suggest_prs_command
from zulip_bot.services.zulip_client import ZulipApiError


def _pr_entry(*, author: str = "zed", labels: list[str], title: str = "a change", queue_age: float = 1000.0) -> dict:
    return {
        "author": author,
        "title": title,
        "labels": [{"name": name} for name in labels],
        "assignees": [],
        "pr_status": "AwaitingReview",
        "total_queue_time": {"status": "valid", "value_td": queue_age},
    }


@override_settings(
    ANALYZER_ASSIGNMENT_SUGGESTIONS_ENABLED=True,
    QUEUEBOARD_BASE_URL="https://queue.example.org",
    ZULIP_BASE_URL="https://leanprover.zulipchat.com",
    ZULIP_BOT_EMAIL="qb-bot@example.com",
    ZULIP_BOT_API_KEY="bot-key",
)
class TestSuggestPrsCommand(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        self.now = timezone.now()
        self.user = User.objects.create(github_login="bob", zulip_user_id=7001)
        ReviewerPreference.objects.create(
            repository=self.repo, user=self.user, preferred_labels=["t-analysis"], maximum_capacity=5
        )
        for name in ("t-analysis", "t-algebra"):
            LabelDef.objects.create(repository=self.repo, name=name, color="ededed")

    def _context(self, *, sender_id: int | None = 7001, content: str = "suggest-prs", is_private: bool = False) -> CommandContext:
        # Stream invocation by default: the case the DM-only delivery exists for.
        return CommandContext(
            sender_id=sender_id,
            sender_email="reviewer@example.com",
            sender_full_name="Reviewer User",
            message_content=content,
            message_id=1,
            stream_id=None if is_private else 42,
            topic=None if is_private else "queue",
            is_private=is_private,
            allowed_command_names=frozenset({"suggest-prs"}),
        )

    def _run(self, args: str = "", **context_kwargs) -> tuple[CommandResult, MagicMock, MagicMock]:
        with (
            patch("zulip_bot.commands.suggest_prs.ZulipClient.send_direct_message") as mock_dm,
            patch("zulip_bot.commands.suggest_prs.ZulipClient.send_stream_message") as mock_stream,
        ):
            result = suggest_prs_command(self._context(**context_kwargs), args)
        return result, mock_dm, mock_stream

    def _dm(self, args: str = "", **context_kwargs) -> str:
        """Run the command and return the DM'd reply, asserting it went only to the sender."""
        result, mock_dm, mock_stream = self._run(args, **context_kwargs)
        self.assertTrue(result.response_not_required)
        self.assertEqual(result.content, "")
        mock_stream.assert_not_called()
        self.assertGreaterEqual(mock_dm.call_count, 1)
        for call in mock_dm.call_args_list:
            self.assertEqual(call.kwargs["to"], [7001])
        return "\n".join(call.kwargs["content"] for call in mock_dm.call_args_list)

    def _in_place(self, args: str = "", **context_kwargs) -> str:
        """Run the command and return an in-place reply, asserting nothing was DM'd."""
        result, mock_dm, mock_stream = self._run(args, **context_kwargs)
        self.assertFalse(result.response_not_required)
        mock_dm.assert_not_called()
        mock_stream.assert_not_called()
        return result.content

    def _seed_snapshot(self, prs: dict[str, dict]) -> None:
        QueueSnapshot.objects.create(
            repository=self.repo,
            cache_key="default",  # no QueueRuleSet in these tests
            generated_at=self.now,
            payload={
                "meta": {"generated_at": self.now.isoformat()},
                "prs": prs,
                "lists": {"dashboards": {"Queue": [int(n) for n in prs]}},
            },
            etag="etag",
            pr_count=len(prs),
            queue_count=len(prs),
        )

    # ---- registry / gating -------------------------------------------------

    def test_aliases_dispatch_to_the_same_command(self) -> None:
        canonical = get_command("suggest-prs")
        self.assertIsNotNone(canonical)
        self.assertIs(get_command("next-pr"), canonical)
        self.assertIs(get_command("suggest-pr"), canonical)

    @override_settings(ANALYZER_ASSIGNMENT_SUGGESTIONS_ENABLED=False)
    def test_flag_gated(self) -> None:
        self.assertIn("not enabled", self._in_place())

    def test_unlinked_sender(self) -> None:
        self.assertIn("No reviewer profile", self._in_place(sender_id=9999))

    def test_missing_sender_id(self) -> None:
        self.assertIn("Could not determine your Zulip identity", self._in_place(sender_id=None))

    # ---- rendering -----------------------------------------------------------

    def test_replies_by_dm_with_load_line_and_pr_lines(self) -> None:
        self._seed_snapshot({"101": _pr_entry(labels=["t-analysis"], title="analysis PR")})
        content = self._dm()
        self.assertIn("Load: 0 / 5 (5 free)", content)
        self.assertIn("[#101](https://github.com/leanprover-community/mathlib4/pull/101): analysis PR", content)
        self.assertIn("`t-analysis`", content)

    # ---- delivery ------------------------------------------------------------

    def test_stream_invocation_is_answered_by_dm_never_in_the_stream(self) -> None:
        # The reply carries private preference signals (the skip tally counts conflict-of-interest
        # exclusions), so a stream invocation must not put it in the stream — neither as the
        # webhook reply (content) nor as a proactive stream message.
        self._seed_snapshot({"102": _pr_entry(labels=["t-algebra"])})
        result, mock_dm, mock_stream = self._run()
        self.assertTrue(result.response_not_required)
        self.assertEqual(result.content, "")
        mock_stream.assert_not_called()
        mock_dm.assert_called_once()
        self.assertEqual(mock_dm.call_args.kwargs["to"], [7001])
        self.assertIn("1 not matching your labels", mock_dm.call_args.kwargs["content"])

    def test_dm_invocation_is_answered_by_dm(self) -> None:
        self._seed_snapshot({"101": _pr_entry(labels=["t-analysis"])})
        self.assertIn("#101", self._dm(is_private=True))

    def test_oversized_reply_sends_every_chunk_by_dm(self) -> None:
        self._seed_snapshot(
            {
                "101": _pr_entry(labels=["t-analysis"], title="first PR", queue_age=3000.0),
                "102": _pr_entry(labels=["t-analysis"], title="second PR", queue_age=2000.0),
                "103": _pr_entry(labels=["t-analysis"], title="third PR", queue_age=1000.0),
            }
        )
        with patch("zulip_bot.commands.suggest_prs.MAX_MESSAGE_CHARS", 120):
            result, mock_dm, mock_stream = self._run()
        self.assertTrue(result.response_not_required)
        mock_stream.assert_not_called()
        self.assertGreater(mock_dm.call_count, 1)
        for call in mock_dm.call_args_list:
            self.assertEqual(call.kwargs["to"], [7001])
            self.assertLessEqual(len(call.kwargs["content"]), 120)
        joined = "\n".join(call.kwargs["content"] for call in mock_dm.call_args_list)
        for title in ("first PR", "second PR", "third PR"):
            self.assertIn(title, joined)

    def test_zulip_api_error_is_reported_in_place_without_the_suggestions(self) -> None:
        self._seed_snapshot({"101": _pr_entry(labels=["t-analysis"], title="analysis PR")})
        with patch("zulip_bot.commands.suggest_prs.ZulipClient.send_direct_message", side_effect=ZulipApiError("rate limited")):
            result = suggest_prs_command(self._context(), "")
        self.assertFalse(result.response_not_required)
        self.assertEqual(result.content, "Failed to send suggestions via Zulip API: rate limited")

    @override_settings(ZULIP_BASE_URL="", ZULIP_BOT_EMAIL="", ZULIP_BOT_API_KEY="")
    def test_unconfigured_zulip_client_is_reported_in_place(self) -> None:
        self._seed_snapshot({"101": _pr_entry(labels=["t-analysis"])})
        result = suggest_prs_command(self._context(), "")
        self.assertFalse(result.response_not_required)
        self.assertIn("Failed to send suggestions via Zulip API", result.content)
        self.assertNotIn("#101", result.content)

    @override_settings(ANALYZER_ASSIGNMENT_SUGGESTIONS_ZULIP_LIMIT=2)
    def test_zulip_limit_caps_the_list(self) -> None:
        self._seed_snapshot(
            {
                "101": _pr_entry(labels=["t-analysis"], queue_age=3000.0),
                "102": _pr_entry(labels=["t-analysis"], queue_age=2000.0),
                "103": _pr_entry(labels=["t-analysis"], queue_age=1000.0),
            }
        )
        content = self._dm()
        self.assertIn("#101", content)
        self.assertIn("#102", content)
        self.assertNotIn("#103", content)

    def test_footer_contents(self) -> None:
        self._seed_snapshot({"101": _pr_entry(labels=["t-analysis"])})
        content = self._dm()
        self.assertIn("`assign #101`", content)
        self.assertIn(
            f"https://queue.example.org/console/suggestions/?repo={self.repo.id}",
            content,
        )
        self.assertIn("<time:", content)
        # Indefinite wording: never a promise the snapshot refresh can break.
        self.assertIn("More suggestions", content)
        self.assertNotIn("next 5", content)

    def test_footer_console_link_carries_the_requested_labels(self) -> None:
        self._seed_snapshot({"102": _pr_entry(labels=["t-algebra"], title="algebra PR")})
        content = self._dm("t-algebra")
        self.assertIn("algebra PR", content)
        self.assertIn(f"?repo={self.repo.id}&labels=t-algebra", content)

    def test_empty_result_renders_the_skip_tally(self) -> None:
        self._seed_snapshot({"102": _pr_entry(labels=["t-algebra"])})
        content = self._dm()
        self.assertIn("No eligible PRs right now", content)
        self.assertIn("1 not matching your labels", content)

    def test_unknown_labels_reported(self) -> None:
        self._seed_snapshot({"101": _pr_entry(labels=["t-analysis"])})
        content = self._dm("t-typo t-analysis")
        self.assertIn("Ignored (not topic labels", content)
        self.assertIn("`t-typo`", content)
        self.assertIn("#101", content)

    @override_settings(ANALYZER_ASSIGNMENT_SUGGESTIONS_MAX_LABELS=1)
    def test_labels_over_the_cap_are_reported_not_silently_ignored(self) -> None:
        self._seed_snapshot({"101": _pr_entry(labels=["t-analysis"])})
        content = self._dm("t-analysis t-algebra")
        self.assertIn("over the per-request label limit", content)
        self.assertIn("`t-algebra`", content)

    def test_no_preferred_labels_hints_at_the_label_override(self) -> None:
        pref = ReviewerPreference.objects.get(user=self.user)
        pref.preferred_labels = []
        pref.save(update_fields=["preferred_labels"])
        self._seed_snapshot({"101": _pr_entry(labels=["t-analysis"])})
        content = self._dm()
        self.assertIn("no preferred labels", content)
        self.assertIn("suggest-prs leanprover-community/mathlib4 t-algebra", content)

    def test_no_snapshot_message(self) -> None:
        content = self._dm()
        self.assertIn("No queue snapshot is available", content)

    # ---- repo argument -------------------------------------------------------

    def test_repo_argument_scopes_the_request(self) -> None:
        other = Repository.objects.create(owner="other", name="repo", default_branch="main")
        ReviewerPreference.objects.create(repository=other, user=self.user, preferred_labels=["t-analysis"])
        self._seed_snapshot({"101": _pr_entry(labels=["t-analysis"])})
        content = self._dm("leanprover-community/mathlib4")
        self.assertIn("#101", content)
        self.assertNotIn("other/repo", content)

    def test_unknown_repo_argument(self) -> None:
        self.assertIn("Unknown repository `nobody/nowhere`", self._in_place("nobody/nowhere"))

    def test_sections_per_repo_when_no_repo_argument(self) -> None:
        other = Repository.objects.create(owner="other", name="repo", default_branch="main")
        ReviewerPreference.objects.create(repository=other, user=self.user, preferred_labels=["t-analysis"])
        self._seed_snapshot({"101": _pr_entry(labels=["t-analysis"])})
        content = self._dm()
        self.assertIn("## leanprover-community/mathlib4", content)
        self.assertIn("## other/repo", content)
        self.assertIn("No queue snapshot is available for other/repo", content)
