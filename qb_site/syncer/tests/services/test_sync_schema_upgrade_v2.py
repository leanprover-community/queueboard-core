"""Tests for the v2 schema upgrader.

Covers ``qb_site/syncer/services/sync_schema_upgrade_v2.py`` —
``UpgradeToV2.is_complete`` / ``UpgradeToV2.kick`` and the boot-time
``register_v2_upgrader`` registration.
"""

from __future__ import annotations

from unittest import mock

from django.test import TestCase

from syncer.models import PullRequest
from syncer.services.sync_schema_upgrade_v2 import UpgradeToV2, register_v2_upgrader
from syncer.services.sync_schema_upgrades import _REGISTRY
from syncer.tests.factories import make_pr, make_repo


class _RegistryIsolationMixin:
    def setUp(self) -> None:  # type: ignore[override]
        super().setUp()
        self._saved_registry = dict(_REGISTRY)
        _REGISTRY.clear()

    def tearDown(self) -> None:  # type: ignore[override]
        _REGISTRY.clear()
        _REGISTRY.update(self._saved_registry)
        super().tearDown()


class UpgradeToV2IsCompleteTests(TestCase):
    def setUp(self) -> None:
        self.repo = make_repo()
        self.pr = make_pr(self.repo, 1)
        self.upgrade = UpgradeToV2()

    def test_is_complete_false_when_backfill_not_done(self) -> None:
        self.assertFalse(self.pr.timeline_backfill_done)
        self.assertFalse(self.upgrade.is_complete(self.pr))

    def test_is_complete_true_when_backfill_done(self) -> None:
        # The pairing migration (0044) resets timeline_backfill_done=False
        # for every v<2 PR at deploy time, so a True observation here
        # always implies a post-deploy rewalk has happened.
        self.pr.timeline_backfill_done = True
        self.pr.save(update_fields=["timeline_backfill_done"])
        self.assertTrue(self.upgrade.is_complete(self.pr))


class UpgradeToV2KickTests(TestCase):
    def setUp(self) -> None:
        self.repo = make_repo()
        # Simulate a v1-era walk that left the PR "fully walked" with a
        # cursor populated; the kick must reset both fields.
        self.pr = make_pr(self.repo, 7)
        PullRequest.objects.filter(pk=self.pr.pk).update(
            timeline_backfill_done=True,
            timeline_backfill_cursor="cursor-from-v1-walk",
        )
        self.pr.refresh_from_db()
        self.upgrade = UpgradeToV2()

    def test_kick_resets_backfill_flags(self) -> None:
        with mock.patch("syncer.tasks.sync_tasks.sync_pr_task.delay"):
            self.upgrade.kick(self.pr)

        # In-memory copy reflects the reset.
        self.assertFalse(self.pr.timeline_backfill_done)
        self.assertIsNone(self.pr.timeline_backfill_cursor)
        # And the persisted row matches.
        self.pr.refresh_from_db()
        self.assertFalse(self.pr.timeline_backfill_done)
        self.assertIsNone(self.pr.timeline_backfill_cursor)

    def test_kick_enqueues_sync_pr_task_with_force_and_backfill_pages(self) -> None:
        with (
            mock.patch("syncer.tasks.sync_tasks.sync_pr_task.delay") as delay_mock,
            self.settings(SYNCER_TIMELINE_BACKFILL_PAGES=4),
        ):
            self.upgrade.kick(self.pr)

        delay_mock.assert_called_once()
        args, kwargs = delay_mock.call_args
        self.assertEqual(args, (self.repo.id, 7))
        self.assertEqual(kwargs.get("force"), True)
        self.assertEqual(kwargs.get("backfill_timeline_pages"), 4)

    def test_kick_uses_default_backfill_pages_when_setting_absent(self) -> None:
        # Default is 2 (see settings/base.py SYNCER_TIMELINE_BACKFILL_PAGES).
        with mock.patch("syncer.tasks.sync_tasks.sync_pr_task.delay") as delay_mock:
            self.upgrade.kick(self.pr)
        _, kwargs = delay_mock.call_args
        # Whatever the project setting is, it must be a positive int — a
        # zero or missing value would silently disable the rewalk.
        self.assertIsInstance(kwargs.get("backfill_timeline_pages"), int)
        self.assertGreater(int(kwargs["backfill_timeline_pages"]), 0)


class RegisterV2UpgraderTests(_RegistryIsolationMixin, TestCase):
    def test_register_is_idempotent(self) -> None:
        # The registry is empty (mixin clears it). First call registers,
        # subsequent calls are no-ops rather than raising.
        register_v2_upgrader()
        register_v2_upgrader()
        register_v2_upgrader()
        registered = _REGISTRY.get(UpgradeToV2.version)
        self.assertIsInstance(registered, UpgradeToV2)
