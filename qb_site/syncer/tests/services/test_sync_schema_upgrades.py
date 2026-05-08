"""Tests for the sync_schema_version upgrader registry and dispatcher.

Covers ``qb_site/syncer/services/sync_schema_upgrades.py``.
"""

from __future__ import annotations

import unittest
from unittest import mock

from django.test import TestCase

from syncer.models import PullRequest
from syncer.services import sync_schema_upgrades
from syncer.services.sync_schema_upgrades import (
    _REGISTRY,
    DispatchOutcome,
    dispatch,
    get_registered,
    register,
    stamp,
)
from syncer.tests.factories import make_pr, make_repo


class _FakeUpgrade:
    """Test double conforming to the ``SchemaUpgrade`` Protocol."""

    def __init__(self, *, version: int, complete: bool = False) -> None:
        self.version = version
        self._complete = complete
        self.is_complete_calls = 0
        self.kick_calls = 0
        self.kick_args: list[PullRequest] = []

    def is_complete(self, pr: PullRequest) -> bool:
        self.is_complete_calls += 1
        return self._complete

    def kick(self, pr: PullRequest) -> None:
        self.kick_calls += 1
        self.kick_args.append(pr)


class _RegistryIsolationMixin:
    """Save / clear / restore the module-level registry around each test."""

    def setUp(self) -> None:  # type: ignore[override]
        super().setUp()
        self._saved_registry = dict(_REGISTRY)
        _REGISTRY.clear()

    def tearDown(self) -> None:  # type: ignore[override]
        _REGISTRY.clear()
        _REGISTRY.update(self._saved_registry)
        super().tearDown()


class RegisterValidationTests(_RegistryIsolationMixin, unittest.TestCase):
    """Pure-Python tests; do not require a database."""

    def test_register_accepts_minimum_valid_version(self) -> None:
        upgrade = register(_FakeUpgrade(version=1))
        self.assertIs(get_registered(1), upgrade)

    def test_register_rejects_duplicate_version(self) -> None:
        register(_FakeUpgrade(version=2))
        with self.assertRaises(ValueError):
            register(_FakeUpgrade(version=2))

    def test_register_rejects_version_below_one(self) -> None:
        with self.assertRaises(ValueError):
            register(_FakeUpgrade(version=0))
        with self.assertRaises(ValueError):
            register(_FakeUpgrade(version=-3))

    def test_get_registered_returns_none_for_unregistered(self) -> None:
        self.assertIsNone(get_registered(99))


class StampTests(_RegistryIsolationMixin, TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = make_repo()
        self.pr = make_pr(self.repo, 1)
        self.assertEqual(self.pr.sync_schema_version, 0)

    def test_stamp_advances_pr_and_returns_true(self) -> None:
        result = stamp(self.pr, 1)
        self.assertTrue(result)
        self.assertEqual(self.pr.sync_schema_version, 1)
        self.pr.refresh_from_db()
        self.assertEqual(self.pr.sync_schema_version, 1)

    def test_stamp_is_idempotent_when_already_at_version(self) -> None:
        stamp(self.pr, 1)
        result = stamp(self.pr, 1)
        self.assertFalse(result)
        self.pr.refresh_from_db()
        self.assertEqual(self.pr.sync_schema_version, 1)

    def test_stamp_does_not_walk_backward(self) -> None:
        # Force the row to v=5 first.
        PullRequest.objects.filter(pk=self.pr.pk).update(sync_schema_version=5)
        self.pr.refresh_from_db()
        result = stamp(self.pr, 3)
        self.assertFalse(result)
        self.pr.refresh_from_db()
        self.assertEqual(self.pr.sync_schema_version, 5)


class DispatchTests(_RegistryIsolationMixin, TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = make_repo()
        self.pr = make_pr(self.repo, 1)

    def test_auto_stamps_missing_upgrader(self) -> None:
        # CURRENT=3 (default), registry empty: a v=0 PR auto-stamps through
        # v=1, v=2, and v=3 in one pass.
        outcome = dispatch(self.pr)
        self.assertIsInstance(outcome, DispatchOutcome)
        self.assertEqual(outcome.stamped_to, 3)
        self.assertEqual(outcome.auto_stamped_versions, (1, 2, 3))
        self.assertFalse(outcome.kicked)
        self.pr.refresh_from_db()
        self.assertEqual(self.pr.sync_schema_version, 3)

    def test_no_op_when_already_at_current(self) -> None:
        stamp(self.pr, 3)
        outcome = dispatch(self.pr)
        self.assertIsNone(outcome.stamped_to)
        self.assertEqual(outcome.auto_stamped_versions, ())
        self.assertFalse(outcome.kicked)

    def test_stamps_when_upgrader_is_complete(self) -> None:
        upgrade = _FakeUpgrade(version=2, complete=True)
        register(upgrade)
        stamp(self.pr, 1)
        with mock.patch.object(sync_schema_upgrades, "CURRENT_SYNC_SCHEMA_VERSION", 2):
            outcome = dispatch(self.pr, kick_budget=1)
        self.assertEqual(outcome.stamped_to, 2)
        self.assertFalse(outcome.kicked)
        self.assertEqual(outcome.auto_stamped_versions, ())
        self.assertEqual(upgrade.is_complete_calls, 1)
        self.assertEqual(upgrade.kick_calls, 0)
        self.pr.refresh_from_db()
        self.assertEqual(self.pr.sync_schema_version, 2)

    def test_kicks_when_upgrader_incomplete(self) -> None:
        upgrade = _FakeUpgrade(version=2, complete=False)
        register(upgrade)
        stamp(self.pr, 1)
        with mock.patch.object(sync_schema_upgrades, "CURRENT_SYNC_SCHEMA_VERSION", 2):
            outcome = dispatch(self.pr, kick_budget=1)
        self.assertIsNone(outcome.stamped_to)
        self.assertTrue(outcome.kicked)
        self.assertEqual(upgrade.kick_calls, 1)
        self.assertEqual(upgrade.kick_args, [self.pr])
        self.pr.refresh_from_db()
        self.assertEqual(self.pr.sync_schema_version, 1)

    def test_does_not_kick_when_budget_zero(self) -> None:
        upgrade = _FakeUpgrade(version=2, complete=False)
        register(upgrade)
        stamp(self.pr, 1)
        with mock.patch.object(sync_schema_upgrades, "CURRENT_SYNC_SCHEMA_VERSION", 2):
            outcome = dispatch(self.pr, kick_budget=0)
        self.assertFalse(outcome.kicked)
        self.assertEqual(upgrade.kick_calls, 0)
        self.assertEqual(upgrade.is_complete_calls, 1)
        self.pr.refresh_from_db()
        self.assertEqual(self.pr.sync_schema_version, 1)

    def test_walks_through_auto_stamps_then_kicks(self) -> None:
        # CURRENT=2, only v=2 has an upgrader; v=1 is auto-stamped on the same pass.
        upgrade = _FakeUpgrade(version=2, complete=False)
        register(upgrade)
        with mock.patch.object(sync_schema_upgrades, "CURRENT_SYNC_SCHEMA_VERSION", 2):
            outcome = dispatch(self.pr, kick_budget=1)
        self.assertEqual(outcome.auto_stamped_versions, (1,))
        self.assertTrue(outcome.kicked)
        self.assertEqual(upgrade.kick_calls, 1)
        self.pr.refresh_from_db()
        self.assertEqual(self.pr.sync_schema_version, 1)

    def test_negative_kick_budget_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            dispatch(self.pr, kick_budget=-1)

    def test_walks_through_multiple_auto_stamps(self) -> None:
        # CURRENT=3, only v=3 registered (complete). Dispatcher must walk
        # through unregistered v=1 and v=2 (auto-stamping) and then stamp v=3.
        upgrade = _FakeUpgrade(version=3, complete=True)
        register(upgrade)
        with mock.patch.object(sync_schema_upgrades, "CURRENT_SYNC_SCHEMA_VERSION", 3):
            outcome = dispatch(self.pr, kick_budget=1)
        self.assertEqual(outcome.stamped_to, 3)
        self.assertEqual(outcome.auto_stamped_versions, (1, 2))
        self.assertFalse(outcome.kicked)
        self.pr.refresh_from_db()
        self.assertEqual(self.pr.sync_schema_version, 3)


class ConcurrentStampTests(_RegistryIsolationMixin, TestCase):
    """Edge cases where two dispatchers race on the same PR.

    The framework relies on the guarded UPDATE in :func:`stamp` (``WHERE pk=?
    AND sync_schema_version < N``) to keep the column monotone in the face of
    stale in-memory views. These tests exercise that contract directly.
    """

    def setUp(self) -> None:
        super().setUp()
        self.repo = make_repo()
        self.pr = make_pr(self.repo, 1)

    def test_stamp_returns_false_when_db_already_at_or_past_version(self) -> None:
        # Simulate "another worker beat us": DB advanced to 5 while our
        # in-memory view is still at 0.
        PullRequest.objects.filter(pk=self.pr.pk).update(sync_schema_version=5)
        # Our in-memory ``self.pr`` is still at 0; stamp(...) must consult the
        # DB guard rather than the stale local value.
        self.assertEqual(self.pr.sync_schema_version, 0)
        self.assertFalse(stamp(self.pr, 5))
        self.assertFalse(stamp(self.pr, 3))
        self.assertEqual(self.pr.sync_schema_version, 0)  # unchanged in-memory
        self.pr.refresh_from_db()
        self.assertEqual(self.pr.sync_schema_version, 5)

    def test_dispatch_handles_stale_pr_view(self) -> None:
        # Another dispatcher already auto-stamped this PR all the way to
        # CURRENT in the DB, but our in-memory snapshot still says v=0.
        # dispatch() should:
        #   - not error,
        #   - not record a phantom auto-stamp (since our UPDATE was a no-op),
        #   - return a clean outcome reflecting "we did nothing new".
        # Note: must stamp DB to CURRENT (=3), not a stale historical value,
        # since the dispatcher walks 1..CURRENT and any step strictly below
        # the DB value would be a real (not phantom) advance.
        PullRequest.objects.filter(pk=self.pr.pk).update(sync_schema_version=3)
        outcome = dispatch(self.pr)
        self.assertIsNone(outcome.stamped_to)
        self.assertEqual(outcome.auto_stamped_versions, ())
        self.assertFalse(outcome.kicked)

    def test_dispatch_does_not_double_kick_when_dbs_state_changes_mid_walk(self) -> None:
        # Setup: CURRENT=2, v=2 upgrader registered (incomplete).
        # Pre-stamp DB to v=1 to simulate "another dispatcher just finished
        # the v=1 auto-stamp leg." Our in-memory pr is still at v=0.
        upgrade = _FakeUpgrade(version=2, complete=False)
        register(upgrade)
        PullRequest.objects.filter(pk=self.pr.pk).update(sync_schema_version=1)
        with mock.patch.object(sync_schema_upgrades, "CURRENT_SYNC_SCHEMA_VERSION", 2):
            outcome = dispatch(self.pr, kick_budget=1)
        # Walk: step 1 stamp returns False (DB ahead), step 2 → kick.
        self.assertEqual(outcome.auto_stamped_versions, ())
        self.assertTrue(outcome.kicked)
        self.assertEqual(upgrade.kick_calls, 1)


class TargetVersionTests(_RegistryIsolationMixin, TestCase):
    """Coverage for the ``target_version`` parameter and the settings gate."""

    def setUp(self) -> None:
        super().setUp()
        self.repo = make_repo()
        self.pr = make_pr(self.repo, 1)

    def test_dispatch_target_version_caps_walk(self) -> None:
        # CURRENT=3 (patched), gate-equivalent target=2: walk must stop at 2.
        with mock.patch.object(sync_schema_upgrades, "CURRENT_SYNC_SCHEMA_VERSION", 3):
            outcome = dispatch(self.pr, target_version=2)
        self.assertEqual(outcome.stamped_to, 2)
        self.assertEqual(outcome.auto_stamped_versions, (1, 2))
        self.assertFalse(outcome.kicked)
        self.pr.refresh_from_db()
        self.assertEqual(self.pr.sync_schema_version, 2)

    def test_dispatch_target_version_clamps_to_current(self) -> None:
        # A misconfigured gate above CURRENT must NOT advance past CURRENT.
        with mock.patch.object(sync_schema_upgrades, "CURRENT_SYNC_SCHEMA_VERSION", 1):
            outcome = dispatch(self.pr, target_version=5)
        self.assertEqual(outcome.stamped_to, 1)
        self.pr.refresh_from_db()
        self.assertEqual(self.pr.sync_schema_version, 1)

    def test_dispatch_target_version_below_initial_is_no_op(self) -> None:
        stamp(self.pr, 2)
        # target=1 < initial=2 — loop range is empty, no work performed.
        with mock.patch.object(sync_schema_upgrades, "CURRENT_SYNC_SCHEMA_VERSION", 3):
            outcome = dispatch(self.pr, target_version=1)
        self.assertIsNone(outcome.stamped_to)
        self.assertEqual(outcome.auto_stamped_versions, ())
        self.assertFalse(outcome.kicked)

    def test_effective_target_version_unset_setting_returns_constant(self) -> None:
        from syncer.services.sync_schema_upgrades import effective_target_version

        with self.settings(SYNCER_SCHEMA_UPGRADE_TARGET_VERSION=None):
            with mock.patch.object(sync_schema_upgrades, "CURRENT_SYNC_SCHEMA_VERSION", 4):
                self.assertEqual(effective_target_version(), 4)

    def test_effective_target_version_caps_at_constant(self) -> None:
        from syncer.services.sync_schema_upgrades import effective_target_version

        with self.settings(SYNCER_SCHEMA_UPGRADE_TARGET_VERSION=99):
            with mock.patch.object(sync_schema_upgrades, "CURRENT_SYNC_SCHEMA_VERSION", 2):
                self.assertEqual(effective_target_version(), 2)

    def test_effective_target_version_holds_below_constant(self) -> None:
        from syncer.services.sync_schema_upgrades import effective_target_version

        with self.settings(SYNCER_SCHEMA_UPGRADE_TARGET_VERSION=1):
            with mock.patch.object(sync_schema_upgrades, "CURRENT_SYNC_SCHEMA_VERSION", 2):
                self.assertEqual(effective_target_version(), 1)
