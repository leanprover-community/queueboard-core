from __future__ import annotations

import io
from unittest import mock

from django.core.management import call_command
from django.test import SimpleTestCase


class TestListChangedPRsCommand(SimpleTestCase):
    def test_outputs_numbers_plain_text(self) -> None:
        # Patch the GitHubClient used by the command module
        with mock.patch("syncer.management.commands.list_changed_prs.GitHubClient") as MockClient:
            instance = MockClient.return_value
            instance.get_changed_pr_numbers.return_value = [10, 9, 8]

            out = io.StringIO()
            call_command(
                "list_changed_prs",
                "--repo",
                "o/r",
                "--since",
                "2025-10-20T00:00:00Z",
                stdout=out,
            )
            self.assertEqual(out.getvalue().strip(), "10\n9\n8")

    def test_json_output_flag(self) -> None:
        with mock.patch("syncer.management.commands.list_changed_prs.GitHubClient") as MockClient:
            instance = MockClient.return_value
            instance.get_changed_pr_numbers.return_value = [1, 2]

            out = io.StringIO()
            call_command(
                "list_changed_prs",
                "--repo",
                "o/r",
                "--since",
                "2025-10-20",
                "--json",
                stdout=out,
            )
            txt = out.getvalue().strip()
            self.assertIn("\"numbers\": [1, 2]", txt)

