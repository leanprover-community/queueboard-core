from __future__ import annotations

from django.test import SimpleTestCase

from zulip_bot.services.assignment_command_parser import (
    AssignmentCommandParseError,
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
