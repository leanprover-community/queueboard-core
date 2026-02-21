from __future__ import annotations

from django.test import TestCase

from core.models import Repository, ReviewerPreference, User
from zulip_bot.commands import CommandContext
from zulip_bot.commands.assign import assign_command
from zulip_bot.commands.unassign import unassign_command


class TestAssignUnassignCommands(TestCase):
    def _context(self, *, sender_id: int | None, rendered_content: str | None = None) -> CommandContext:
        return CommandContext(
            sender_id=sender_id,
            sender_email="reviewer@example.com",
            sender_full_name="Reviewer User",
            message_content="assign",
            message_id=1,
            stream_id=None,
            topic=None,
            is_private=True,
            rendered_content=rendered_content,
            allowed_command_names=frozenset({"assign", "unassign"}),
        )

    def test_assign_reports_parse_error_when_pr_missing(self) -> None:
        result = assign_command(self._context(sender_id=101), "@**Reviewer**")
        self.assertIn("Could not parse `assign` command", result.content)
        self.assertIn("No GitHub pull request link found", result.content)

    def test_assign_preflight_passes_with_sender_fallback(self) -> None:
        repo = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        user = User.objects.create(zulip_user_id=101, github_login="reviewer")
        ReviewerPreference.objects.create(repository=repo, user=user)

        result = assign_command(
            self._context(sender_id=101),
            "https://github.com/leanprover-community/mathlib4/pull/22",
        )

        self.assertIn("Validated targets: `reviewer`.", result.content)
        self.assertIn("Preflight passed for `assign` on leanprover-community/mathlib4#22.", result.content)

    def test_assign_reports_unresolved_mentions(self) -> None:
        result = assign_command(
            self._context(sender_id=101),
            "https://github.com/leanprover-community/mathlib4/pull/22 @**Unknown Person**",
        )

        self.assertIn("Warnings:", result.content)
        self.assertIn("Unresolved mentions: Unknown Person.", result.content)
        self.assertIn("Failures:", result.content)
        self.assertIn("No valid reviewers to assign after validation.", result.content)

    def test_unassign_reports_validation_failures(self) -> None:
        Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        User.objects.create(zulip_user_id=101, github_login="reviewer")

        result = unassign_command(
            self._context(sender_id=101),
            "https://github.com/leanprover-community/mathlib4/pull/22",
        )

        self.assertIn("Failures:", result.content)
        self.assertIn("missing_preference", result.content)
        self.assertIn("|101**", result.content)
        self.assertIn("No valid reviewers to unassign after validation.", result.content)

    def test_assign_prefers_rendered_mentions_over_raw_mentions(self) -> None:
        repo = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        sender = User.objects.create(zulip_user_id=101, github_login="sender")
        target = User.objects.create(zulip_user_id=202, github_login="target")
        ReviewerPreference.objects.create(repository=repo, user=sender)
        ReviewerPreference.objects.create(repository=repo, user=target)

        rendered_content = (
            '<p>@<span class="user-mention" data-user-id="202">Target User</span> '
            '<a href="https://github.com/leanprover-community/mathlib4/pull/33">#33</a></p>'
        )
        result = assign_command(
            self._context(sender_id=101, rendered_content=rendered_content),
            "#33 @**Target User**",
        )

        self.assertIn("Validated targets: `target`.", result.content)
        self.assertNotIn("`sender`", result.content)

    def test_assign_unknown_reviewer_uses_mentioned_name_in_failure(self) -> None:
        Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        rendered_content = (
            '<p>@<span class="user-mention" data-user-id="999">Brand New Reviewer</span> '
            '<a href="https://github.com/leanprover-community/mathlib4/pull/34">#34</a></p>'
        )
        result = assign_command(
            self._context(sender_id=101, rendered_content=rendered_content),
            "#34 @**Brand New Reviewer**",
        )

        self.assertIn("@_**Brand New Reviewer|999**", result.content)
        self.assertIn("unknown_reviewer", result.content)
