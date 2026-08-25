from __future__ import annotations

from django.test import TestCase, override_settings
from django.utils import timezone

from analyzer.models import QueueSnapshot
from core.models import Repository, ReviewerPreference, User
from syncer.models import LabelDef
from zulip_bot.commands import CommandContext, get_command
from zulip_bot.commands.suggest_prs import suggest_prs_command


def _pr_entry(*, author: str = "zed", labels: list[str], title: str = "a change", queue_age: float = 1000.0) -> dict:
    return {
        "author": author,
        "title": title,
        "labels": [{"name": name} for name in labels],
        "assignees": [],
        "pr_status": "AwaitingReview",
        "total_queue_time": {"status": "valid", "value_td": queue_age},
    }


@override_settings(ANALYZER_ASSIGNMENT_SUGGESTIONS_ENABLED=True, QUEUEBOARD_BASE_URL="https://queue.example.org")
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

    def _context(self, *, sender_id: int | None = 7001, content: str = "suggest-prs") -> CommandContext:
        return CommandContext(
            sender_id=sender_id,
            sender_email="reviewer@example.com",
            sender_full_name="Reviewer User",
            message_content=content,
            message_id=1,
            stream_id=42,
            topic="queue",
            is_private=False,
            allowed_command_names=frozenset({"suggest-prs"}),
        )

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
        result = suggest_prs_command(self._context(), "")
        self.assertIn("not enabled", result.content)

    def test_unlinked_sender(self) -> None:
        result = suggest_prs_command(self._context(sender_id=9999), "")
        self.assertIn("No reviewer profile", result.content)

    # ---- rendering -----------------------------------------------------------

    def test_replies_in_place_with_load_line_and_pr_lines(self) -> None:
        self._seed_snapshot({"101": _pr_entry(labels=["t-analysis"], title="analysis PR")})
        result = suggest_prs_command(self._context(), "")
        self.assertFalse(result.response_not_required)  # in-place reply, not a proactive DM
        self.assertIn("Load: 0 / 5 (5 free)", result.content)
        self.assertIn("[#101](https://github.com/leanprover-community/mathlib4/pull/101): analysis PR", result.content)
        self.assertIn("`t-analysis`", result.content)

    @override_settings(ANALYZER_ASSIGNMENT_SUGGESTIONS_ZULIP_LIMIT=2)
    def test_zulip_limit_caps_the_list(self) -> None:
        self._seed_snapshot(
            {
                "101": _pr_entry(labels=["t-analysis"], queue_age=3000.0),
                "102": _pr_entry(labels=["t-analysis"], queue_age=2000.0),
                "103": _pr_entry(labels=["t-analysis"], queue_age=1000.0),
            }
        )
        result = suggest_prs_command(self._context(), "")
        self.assertIn("#101", result.content)
        self.assertIn("#102", result.content)
        self.assertNotIn("#103", result.content)

    def test_footer_contents(self) -> None:
        self._seed_snapshot({"101": _pr_entry(labels=["t-analysis"])})
        result = suggest_prs_command(self._context(), "")
        self.assertIn("`assign #101`", result.content)
        self.assertIn(
            f"https://queue.example.org/console/suggestions/?repo={self.repo.id}",
            result.content,
        )
        self.assertIn("<time:", result.content)
        # Indefinite wording: never a promise the snapshot refresh can break.
        self.assertIn("More suggestions", result.content)
        self.assertNotIn("next 5", result.content)

    def test_footer_console_link_carries_the_requested_labels(self) -> None:
        self._seed_snapshot({"102": _pr_entry(labels=["t-algebra"], title="algebra PR")})
        result = suggest_prs_command(self._context(), "t-algebra")
        self.assertIn("algebra PR", result.content)
        self.assertIn(f"?repo={self.repo.id}&labels=t-algebra", result.content)

    def test_empty_result_renders_the_skip_tally(self) -> None:
        self._seed_snapshot({"102": _pr_entry(labels=["t-algebra"])})
        result = suggest_prs_command(self._context(), "")
        self.assertIn("No eligible PRs right now", result.content)
        self.assertIn("1 not matching your labels", result.content)

    def test_unknown_labels_reported(self) -> None:
        self._seed_snapshot({"101": _pr_entry(labels=["t-analysis"])})
        result = suggest_prs_command(self._context(), "t-typo t-analysis")
        self.assertIn("Ignored (not topic labels", result.content)
        self.assertIn("`t-typo`", result.content)
        self.assertIn("#101", result.content)

    def test_no_preferred_labels_hints_at_the_label_override(self) -> None:
        pref = ReviewerPreference.objects.get(user=self.user)
        pref.preferred_labels = []
        pref.save(update_fields=["preferred_labels"])
        self._seed_snapshot({"101": _pr_entry(labels=["t-analysis"])})
        result = suggest_prs_command(self._context(), "")
        self.assertIn("no preferred labels", result.content)
        self.assertIn("suggest-prs leanprover-community/mathlib4 t-algebra", result.content)

    def test_no_snapshot_message(self) -> None:
        result = suggest_prs_command(self._context(), "")
        self.assertIn("No queue snapshot is available", result.content)

    # ---- repo argument -------------------------------------------------------

    def test_repo_argument_scopes_the_request(self) -> None:
        other = Repository.objects.create(owner="other", name="repo", default_branch="main")
        ReviewerPreference.objects.create(repository=other, user=self.user, preferred_labels=["t-analysis"])
        self._seed_snapshot({"101": _pr_entry(labels=["t-analysis"])})
        result = suggest_prs_command(self._context(), "leanprover-community/mathlib4")
        self.assertIn("#101", result.content)
        self.assertNotIn("other/repo", result.content)

    def test_unknown_repo_argument(self) -> None:
        result = suggest_prs_command(self._context(), "nobody/nowhere")
        self.assertIn("Unknown repository `nobody/nowhere`", result.content)

    def test_sections_per_repo_when_no_repo_argument(self) -> None:
        other = Repository.objects.create(owner="other", name="repo", default_branch="main")
        ReviewerPreference.objects.create(repository=other, user=self.user, preferred_labels=["t-analysis"])
        self._seed_snapshot({"101": _pr_entry(labels=["t-analysis"])})
        result = suggest_prs_command(self._context(), "")
        self.assertIn("## leanprover-community/mathlib4", result.content)
        self.assertIn("## other/repo", result.content)
        self.assertIn("No queue snapshot is available for other/repo", result.content)
