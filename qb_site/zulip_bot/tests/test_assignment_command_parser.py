from __future__ import annotations

from django.test import SimpleTestCase

from zulip_bot.services.assignment_command_parser import (
    AssignmentCommandParseError,
    _parse_single_issue_or_pr_ref,
    parse_assignment_command_args,
)


class TestAssignmentCommandParser(SimpleTestCase):
    def test_parses_pr_from_rendered_linkifier_anchor(self) -> None:
        rendered_content = (
            '<p>@<span class="user-mention" data-user-id="777">Reviewer</span> '
            '<a href="https://github.com/leanprover-community/mathlib4/pull/12345">#12345</a></p>'
        )

        parsed = parse_assignment_command_args(
            args="#12345 @**Reviewer**",
            rendered_content=rendered_content,
            sender_id=101,
        )

        self.assertEqual(parsed.pr.owner, "leanprover-community")
        self.assertEqual(parsed.pr.repo, "mathlib4")
        self.assertEqual(parsed.pr.number, 12345)
        self.assertEqual(parsed.target_user_ids, (777,))
        self.assertEqual(parsed.unresolved_mentions, ())
        self.assertEqual(parsed.mention_labels_by_user_id, ((777, "Reviewer"),))

    def test_falls_back_to_sender_when_mentions_omitted(self) -> None:
        parsed = parse_assignment_command_args(
            args="https://github.com/leanprover-community/mathlib4/pull/22",
            rendered_content="",
            sender_id=555,
        )

        self.assertEqual(parsed.target_user_ids, (555,))

    def test_returns_unresolved_when_mentions_present_but_unresolved(self) -> None:
        parsed = parse_assignment_command_args(
            args="https://github.com/leanprover-community/mathlib4/pull/22 @**Unknown Person**",
            rendered_content="<p>command with broken mention</p>",
            sender_id=555,
        )

        self.assertEqual(parsed.target_user_ids, ())
        self.assertEqual(parsed.unresolved_mentions, ("Unknown Person",))

    def test_rejects_missing_pr(self) -> None:
        with self.assertRaises(AssignmentCommandParseError) as exc:
            parse_assignment_command_args(
                args="@**Reviewer**",
                rendered_content='<p>@<span class="user-mention" data-user-id="4">Reviewer</span></p>',
                sender_id=1,
            )

        self.assertEqual(exc.exception.code, "missing_pr")

    def test_rejects_multiple_distinct_prs(self) -> None:
        rendered_content = (
            '<a href="https://github.com/leanprover-community/mathlib4/pull/10">#10</a> '
            '<a href="https://github.com/leanprover-community/batteries/pull/20">#20</a>'
        )
        with self.assertRaises(AssignmentCommandParseError) as exc:
            parse_assignment_command_args(
                args="assign please",
                rendered_content=rendered_content,
                sender_id=1,
            )

        self.assertEqual(exc.exception.code, "ambiguous_pr")

    def test_requires_sender_for_default_when_mentions_omitted(self) -> None:
        with self.assertRaises(AssignmentCommandParseError) as exc:
            parse_assignment_command_args(
                args="https://github.com/leanprover-community/mathlib4/pull/99",
                rendered_content="",
                sender_id=None,
            )

        self.assertEqual(exc.exception.code, "missing_sender")

    def test_rejects_unexpected_non_mention_trailing_tokens(self) -> None:
        with self.assertRaises(AssignmentCommandParseError) as exc:
            parse_assignment_command_args(
                args="https://github.com/leanprover-community/mathlib4/pull/22 definitely-not-a-mention",
                rendered_content="",
                sender_id=555,
            )

        self.assertEqual(exc.exception.code, "unexpected_args")
        self.assertIn("definitely-not-a-mention", str(exc.exception))

    def test_silent_mention_is_treated_as_mention_not_unexpected_text(self) -> None:
        parsed = parse_assignment_command_args(
            args="https://github.com/leanprover-community/mathlib4/pull/22 @_**Unknown Person**",
            rendered_content="",
            sender_id=555,
        )

        self.assertEqual(parsed.target_user_ids, ())
        self.assertEqual(parsed.unresolved_mentions, ("Unknown Person",))

    def test_ignores_leading_bot_mention_from_rendered_mentions(self) -> None:
        rendered_content = (
            '<p>@<span class="user-mention" data-user-id="500">queueboard-bot</span> '
            '@<span class="user-mention" data-user-id="777">Reviewer</span> '
            '<a href="https://github.com/leanprover-community/mathlib4/pull/12345">#12345</a></p>'
        )
        parsed = parse_assignment_command_args(
            args="#12345 @**Reviewer**",
            rendered_content=rendered_content,
            sender_id=101,
        )

        self.assertEqual(parsed.target_user_ids, (777,))


class TestParseIssueOrPRRef(SimpleTestCase):
    """Tests for the issue/PR ref parser used by the label-pr command."""

    def test_accepts_pull_url(self) -> None:
        ref = _parse_single_issue_or_pr_ref(
            args="https://github.com/leanprover-community/mathlib4/pull/42",
            rendered_content=None,
        )
        self.assertEqual(ref.owner, "leanprover-community")
        self.assertEqual(ref.repo, "mathlib4")
        self.assertEqual(ref.number, 42)

    def test_accepts_issues_url(self) -> None:
        ref = _parse_single_issue_or_pr_ref(
            args="https://github.com/leanprover-community/mathlib4/issues/42",
            rendered_content=None,
        )
        self.assertEqual(ref.owner, "leanprover-community")
        self.assertEqual(ref.repo, "mathlib4")
        self.assertEqual(ref.number, 42)

    def test_raises_on_no_url(self) -> None:
        with self.assertRaises(AssignmentCommandParseError) as cm:
            _parse_single_issue_or_pr_ref(args="no-url-here", rendered_content=None)
        self.assertEqual(cm.exception.code, "missing_pr")
