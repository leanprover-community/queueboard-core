from __future__ import annotations

import io
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from core.models import Repository
from syncer.models import PullRequest
from syncer.tests.factories import make_repo, make_pr


class TestSyncRepoCommand(TestCase):
    def setUp(self) -> None:
        self.repo = make_repo()

    def _make_pr(self, number: int, last_synced_at=None):
        if last_synced_at is None:
            last_synced_at = timezone.now()
        return make_pr(self.repo, number, last_synced_at=last_synced_at)

    def test_since_discovery_triggers_sync(self) -> None:
        # Patch GitHubClient and PRSyncService used inside the command module
        with (
            mock.patch("syncer.management.commands.sync_repo.GitHubClient") as MockClient,
            mock.patch("syncer.management.commands.sync_repo.PRSyncService") as MockSvc,
        ):
            gh = MockClient.return_value
            gh.get_changed_pr_numbers.return_value = [42]
            gh.get_pr_header.return_value = {
                "data": {"repository": {"pullRequest": {"number": 42, "updatedAt": "2025-10-21T00:00:00Z"}}}
            }
            svc = MockSvc.return_value
            svc.sync_pull_request.return_value = {
                "labels_created": 0,
                "labels_updated": 0,
                "prlabels_created": 0,
                "prlabels_deleted": 0,
                "events_created": 0,
                "checkruns_upserted": 0,
                "statusctx_upserted": 0,
            }

            out = io.StringIO()
            call_command(
                "sync_repo",
                "--repo",
                "o/r",
                "--since",
                "2025-10-20T00:00:00Z",
                stdout=out,
            )
            # Ensure discovered PR was synced
            svc.sync_pull_request.assert_called()
            args, kwargs = svc.sync_pull_request.call_args
            self.assertEqual(kwargs.get("number"), 42)
            self.assertIn("Synced PR #42", out.getvalue())

    def test_preflight_skips_up_to_date(self) -> None:
        # Existing PR with recent last_synced_at
        pr = self._make_pr(1, last_synced_at=timezone.now())

        with (
            mock.patch("syncer.management.commands.sync_repo.GitHubClient") as MockClient,
            mock.patch("syncer.management.commands.sync_repo.PRSyncService") as MockSvc,
        ):
            gh = MockClient.return_value
            # updatedAt earlier than last_synced_at → skip
            gh.get_pr_header.return_value = {
                "data": {
                    "repository": {
                        "pullRequest": {"number": 1, "updatedAt": (pr.last_synced_at - timezone.timedelta(minutes=1)).isoformat()}
                    }
                }
            }
            svc = MockSvc.return_value

            out = io.StringIO()
            call_command(
                "sync_repo",
                "--repo",
                "o/r",
                "--number",
                "1",
                stdout=out,
            )
            # Ensure sync not called and output mentions skipping
            svc.sync_pull_request.assert_not_called()
            self.assertIn("up-to-date; skipping", out.getvalue())

    def test_rate_limit_reset_printed_once(self) -> None:
        with (
            mock.patch("syncer.management.commands.sync_repo.GitHubClient") as MockClient,
            mock.patch("syncer.management.commands.sync_repo.PRSyncService") as MockSvc,
        ):
            gh = MockClient.return_value
            gh.get_pr_header.return_value = {
                "data": {"repository": {"pullRequest": {"number": 7, "updatedAt": "2025-10-21T00:00:00Z"}}}
            }
            # Same resetAt twice -> should print only once
            gh.get_last_rate_limit.side_effect = [
                {"resetAt": "2025-11-01T00:00:00Z"},
                {"resetAt": "2025-11-01T00:00:00Z"},
            ]
            svc = MockSvc.return_value
            svc.sync_pull_request.return_value = {
                "labels_created": 0,
                "labels_updated": 0,
                "prlabels_created": 0,
                "prlabels_deleted": 0,
                "events_created": 0,
                "checkruns_upserted": 0,
                "statusctx_upserted": 0,
            }

            out = io.StringIO()
            call_command(
                "sync_repo",
                "--repo",
                "o/r",
                "--number",
                "7",
                stdout=out,
            )
            txt = out.getvalue()
            self.assertEqual(txt.count("rateLimit.resetAt: 2025-11-01T00:00:00Z"), 1)
