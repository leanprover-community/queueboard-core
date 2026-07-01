"""Tests for the resync_archive_touched_tick beat task (design doc 043 follow-up)."""

from __future__ import annotations

from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from syncer.models import ArchiveImportItem, ArchiveImportItemStatus
from syncer.tasks.archive_import import resync_archive_touched_tick
from syncer.tests.factories import make_pr, make_repo

TASKS_MOD = "syncer.tasks.archive_import"


class TestResyncArchiveTouchedTick(TestCase):
    def setUp(self) -> None:
        self.repo = make_repo(owner="leanprover-community", name="mathlib4")
        self.touch = timezone.now() - timezone.timedelta(days=7)

    def _target(self, number: int, *, state: str = "closed", last_synced_at=...):
        pr = make_pr(
            self.repo,
            number,
            state=state,
            last_synced_at=(self.touch - timezone.timedelta(days=1)) if last_synced_at is ... else last_synced_at,
        )
        ArchiveImportItem.objects.create(
            repository=self.repo,
            archive_name="queueboard-archive2",
            pr_number=number,
            archive_path=f"data/{number}/pr_info.json",
            status=ArchiveImportItemStatus.COMPLETED,
            completed_at=self.touch,
        )
        return pr

    def _run(self):
        return resync_archive_touched_tick()

    @mock.patch(f"{TASKS_MOD}.sync_pr_task")
    def test_disabled_by_default(self, task) -> None:
        self._target(1)
        result = self._run()
        self.assertEqual(result, {"status": "disabled", "enqueued": 0})
        task.delay.assert_not_called()

    @override_settings(ARCHIVE_RESYNC_PER_TICK=5)
    @mock.patch(f"{TASKS_MOD}.sync_pr_task")
    def test_drained_when_no_targets(self, task) -> None:
        result = self._run()
        self.assertEqual(result["status"], "drained")
        task.delay.assert_not_called()

    @override_settings(ARCHIVE_RESYNC_PER_TICK=2)
    @mock.patch(f"{TASKS_MOD}.claim_enqueue_slot", return_value=True)
    @mock.patch(f"{TASKS_MOD}.get_rate_snapshot", return_value={"remaining": 5000})
    @mock.patch(f"{TASKS_MOD}.GitHubClient")
    @mock.patch(f"{TASKS_MOD}.sync_pr_task")
    def test_enqueues_up_to_per_tick_in_target_order(self, task, client, snapshot, claim) -> None:
        client.return_value.token_id = "tok"
        self._target(1, state="closed")
        self._target(2, state="open")
        self._target(3, state="open", last_synced_at=None)
        result = self._run()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["enqueued"], 2)
        self.assertEqual(result["remaining"], 3)
        # Open first, NULL last_synced_at first; per_tick=2 stops before the closed PR.
        self.assertEqual(
            task.delay.call_args_list,
            [mock.call(self.repo.id, 3, force=True), mock.call(self.repo.id, 2, force=True)],
        )

    @override_settings(ARCHIVE_RESYNC_PER_TICK=5)
    @mock.patch(f"{TASKS_MOD}.claim_enqueue_slot", return_value=True)
    @mock.patch(f"{TASKS_MOD}.get_rate_snapshot", return_value={"remaining": 5000})
    @mock.patch(f"{TASKS_MOD}.GitHubClient")
    @mock.patch(f"{TASKS_MOD}.sync_pr_task")
    def test_healed_pr_is_not_targeted(self, task, client, snapshot, claim) -> None:
        client.return_value.token_id = "tok"
        self._target(1, last_synced_at=self.touch + timezone.timedelta(days=1))
        result = self._run()
        self.assertEqual(result["status"], "drained")
        task.delay.assert_not_called()

    @override_settings(ARCHIVE_RESYNC_PER_TICK=5, ARCHIVE_RESYNC_MIN_RATE_REMAINING=2500)
    @mock.patch(f"{TASKS_MOD}.claim_enqueue_slot", return_value=True)
    @mock.patch(f"{TASKS_MOD}.get_rate_snapshot", return_value={"remaining": 200})
    @mock.patch(f"{TASKS_MOD}.GitHubClient")
    @mock.patch(f"{TASKS_MOD}.sync_pr_task")
    def test_skips_tick_when_rate_budget_low(self, task, client, snapshot, claim) -> None:
        client.return_value.token_id = "tok"
        self._target(1)
        self._target(2)
        result = self._run()
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["enqueued"], 0)
        self.assertEqual(result["skipped_rate_budget"], 2)
        task.delay.assert_not_called()

    @override_settings(ARCHIVE_RESYNC_PER_TICK=5)
    @mock.patch(f"{TASKS_MOD}.claim_enqueue_slot", return_value=True)
    @mock.patch(f"{TASKS_MOD}.GitHubClient", side_effect=RuntimeError("no token"))
    @mock.patch(f"{TASKS_MOD}.sync_pr_task")
    def test_fails_open_without_rate_snapshot(self, task, client, claim) -> None:
        # No token / no snapshot → proceed; sync_pr has its own deferral.
        self._target(1)
        result = self._run()
        self.assertEqual(result["enqueued"], 1)
        task.delay.assert_called_once_with(self.repo.id, 1, force=True)

    @override_settings(ARCHIVE_RESYNC_PER_TICK=1)
    @mock.patch(f"{TASKS_MOD}.claim_enqueue_slot", side_effect=[False, True])
    @mock.patch(f"{TASKS_MOD}.get_rate_snapshot", return_value={"remaining": 5000})
    @mock.patch(f"{TASKS_MOD}.GitHubClient")
    @mock.patch(f"{TASKS_MOD}.sync_pr_task")
    def test_dedupe_skip_does_not_consume_chunk(self, task, client, snapshot, claim) -> None:
        client.return_value.token_id = "tok"
        self._target(1, state="open", last_synced_at=None)
        self._target(2, state="open")
        result = self._run()
        # First target is already in flight (slot held); the over-fetched second
        # target still fills the chunk.
        self.assertEqual(result["enqueued"], 1)
        self.assertEqual(result["skipped_dedupe"], 1)
        task.delay.assert_called_once_with(self.repo.id, 2, force=True)
