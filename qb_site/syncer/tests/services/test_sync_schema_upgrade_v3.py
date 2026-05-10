"""Tests for the v3 schema upgrader.

Covers ``qb_site/syncer/services/sync_schema_upgrade_v3.py`` —
``UpgradeToV3.is_complete`` / ``UpgradeToV3.kick`` and the boot-time
``register_v3_upgrader`` registration.

The v3 upgrader inherits both methods from :class:`UpgradeToV2`; these
tests confirm the inheritance is intact and that registration targets
the v=3 slot rather than v=2.
"""

from __future__ import annotations

from unittest import mock

from django.test import TestCase

from syncer.models import PullRequest
from syncer.services.sync_schema_upgrade_v2 import UpgradeToV2
from syncer.services.sync_schema_upgrade_v3 import UpgradeToV3, register_v3_upgrader
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


class UpgradeToV3IsCompleteTests(TestCase):
    def setUp(self) -> None:
        self.repo = make_repo()
        self.pr = make_pr(self.repo, 1)
        self.upgrade = UpgradeToV3()

    def test_version_is_three(self) -> None:
        self.assertEqual(self.upgrade.version, 3)

    def test_inherits_from_v2(self) -> None:
        # Inheritance guarantees is_complete/kick stay in sync with v2 unless
        # explicitly overridden — the v3 wave is mechanically the same rewalk
        # as v2, just re-targeted at PRs already stamped to v=2.
        self.assertIsInstance(self.upgrade, UpgradeToV2)

    def test_is_complete_false_when_backfill_not_done(self) -> None:
        self.assertFalse(self.pr.timeline_backfill_done)
        self.assertFalse(self.upgrade.is_complete(self.pr))

    def test_is_complete_true_when_backfill_done(self) -> None:
        # The pairing migration (0045) resets timeline_backfill_done=False
        # for every v<3 PR at deploy time, so a True observation here
        # always implies a post-deploy rewalk under the fixed page paths.
        self.pr.timeline_backfill_done = True
        self.pr.save(update_fields=["timeline_backfill_done"])
        self.assertTrue(self.upgrade.is_complete(self.pr))


class UpgradeToV3KickTests(TestCase):
    def setUp(self) -> None:
        self.repo = make_repo()
        # Simulate a v=2 walk that left the PR "fully walked" with a cursor
        # populated — this is the exact state we expect for PRs that hit the
        # v2 wire-up gap. Migration 0045 will reset both fields, so kick
        # itself just enqueues the rewalk.
        self.pr = make_pr(self.repo, 7)
        PullRequest.objects.filter(pk=self.pr.pk).update(
            sync_schema_version=2,
            timeline_backfill_done=True,
            timeline_backfill_cursor="cursor-from-v2-walk",
        )
        self.pr.refresh_from_db()
        self.upgrade = UpgradeToV3()

    def test_kick_resets_backfill_flags(self) -> None:
        with mock.patch("syncer.tasks.sync_tasks.sync_pr_task.delay"):
            self.upgrade.kick(self.pr)

        self.assertFalse(self.pr.timeline_backfill_done)
        self.assertIsNone(self.pr.timeline_backfill_cursor)
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


class RegisterV3UpgraderTests(_RegistryIsolationMixin, TestCase):
    def test_register_is_idempotent(self) -> None:
        register_v3_upgrader()
        register_v3_upgrader()
        register_v3_upgrader()
        registered = _REGISTRY.get(UpgradeToV3.version)
        self.assertIsInstance(registered, UpgradeToV3)

    def test_register_targets_v3_slot_not_v2(self) -> None:
        # Co-existence sanity: v2 and v3 must occupy distinct registry slots.
        from syncer.services.sync_schema_upgrade_v2 import register_v2_upgrader

        register_v2_upgrader()
        register_v3_upgrader()
        v2 = _REGISTRY.get(2)
        v3 = _REGISTRY.get(3)
        self.assertIsInstance(v2, UpgradeToV2)
        self.assertIsInstance(v3, UpgradeToV3)
        self.assertIsNot(v2, v3)
        # And v3 is the more-derived class, not a v2 instance.
        self.assertNotIsInstance(v2, UpgradeToV3)
