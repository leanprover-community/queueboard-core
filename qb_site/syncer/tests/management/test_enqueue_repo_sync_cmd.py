from __future__ import annotations

import io
from unittest import mock

from django.core.management import call_command
from django.test import TestCase

from core.models import Repository
from syncer.tests.factories import make_repo


class TestEnqueueRepoSyncCommand(TestCase):
    def setUp(self) -> None:
        make_repo()

    @mock.patch("syncer.management.commands.enqueue_repo_sync.sync_repo_since_task")
    def test_enqueue_repo_sync_minimal(self, mock_task) -> None:
        mock_task.delay.return_value.id = "abc123"
        out = io.StringIO()
        call_command("enqueue_repo_sync", "--repo", "o/r", stdout=out)
        self.assertIn("Enqueued sync_repo_since for o/r", out.getvalue())
        mock_task.delay.assert_called_once()

    @mock.patch("syncer.management.commands.enqueue_repo_sync.sync_repo_since_task")
    def test_enqueue_repo_sync_with_args(self, mock_task) -> None:
        mock_task.delay.return_value.id = "xyz789"
        out = io.StringIO()
        call_command(
            "enqueue_repo_sync",
            "--repo",
            "o/r",
            "--since",
            "2025-10-20T00:00:00Z",
            "--limit",
            "10",
            "--states",
            "OPEN",
            "--timelineK",
            "200",
            "--commitsM",
            "20",
            "--dry-run",
            stdout=out,
        )
        _, kwargs = mock_task.delay.call_args
        self.assertEqual(kwargs.get("since_iso"), "2025-10-20T00:00:00Z")
        self.assertEqual(kwargs.get("limit"), 10)
        self.assertEqual(kwargs.get("states"), ["OPEN"])
        self.assertEqual(kwargs.get("timelineK"), 200)
        self.assertEqual(kwargs.get("commitsM"), 20)
        self.assertTrue(kwargs.get("dry_run"))
