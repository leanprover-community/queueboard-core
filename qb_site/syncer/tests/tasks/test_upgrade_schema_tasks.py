"""Tests for ``syncer.upgrade_schema_versions`` Celery tasks.

Covers ``qb_site/syncer/tasks/upgrade_schema_tasks.py``.
"""

from __future__ import annotations

from unittest import mock

from django.test import TestCase

from syncer.models import PullRequest
from syncer.services import sync_schema_upgrades
from syncer.services.sync_schema_upgrades import _REGISTRY, register, stamp
from syncer.tasks.upgrade_schema_tasks import (
    upgrade_schema_versions_active_task,
    upgrade_schema_versions_task,
)
from syncer.tests.factories import make_pr, make_repo


class _FakeUpgrade:
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
    def setUp(self) -> None:  # type: ignore[override]
        super().setUp()
        self._saved_registry = dict(_REGISTRY)
        _REGISTRY.clear()

    def tearDown(self) -> None:  # type: ignore[override]
        _REGISTRY.clear()
        _REGISTRY.update(self._saved_registry)
        super().tearDown()


def _versions(repo) -> list[int]:
    """Return ``sync_schema_version`` for each PR in ``repo`` ordered by ``number``."""
    return list(PullRequest.objects.filter(repository=repo).order_by("number").values_list("sync_schema_version", flat=True))


class TestUpgradeSchemaVersionsTask(_RegistryIsolationMixin, TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = make_repo()

    def test_no_candidates_returns_zero_counts(self) -> None:
        pr = make_pr(self.repo, 1)
        stamp(pr, 2)  # already at CURRENT (=2) → not a candidate

        res = upgrade_schema_versions_task(self.repo.id, batch_size=10, kick_limit=5)

        self.assertEqual(res["considered"], 0)
        self.assertEqual(res["stamped"], 0)
        self.assertEqual(res["kicked"], 0)
        self.assertEqual(res["target"], 2)
        self.assertEqual(res["repo_id"], self.repo.id)

    def test_auto_stamps_v0_prs_in_one_pass(self) -> None:
        # Registry is cleared by the mixin, so the dispatcher walks each PR
        # 0 → 1 → 2 in a single pass, recording two auto-stamps per PR.
        for n in range(1, 4):
            make_pr(self.repo, n)

        res = upgrade_schema_versions_task(self.repo.id, batch_size=10, kick_limit=5)

        self.assertEqual(res["considered"], 3)
        self.assertEqual(res["stamped"], 3)
        self.assertEqual(res["auto_stamped"], 6)
        self.assertEqual(res["kicked"], 0)
        self.assertEqual(res["kick_budget_remaining"], 5)
        self.assertEqual(_versions(self.repo), [2, 2, 2])

    def test_respects_batch_size(self) -> None:
        for n in range(1, 6):
            make_pr(self.repo, n)

        res = upgrade_schema_versions_task(self.repo.id, batch_size=2, kick_limit=5)

        self.assertEqual(res["considered"], 2)
        self.assertEqual(res["stamped"], 2)
        # Three PRs still at v=0; the next pass picks them up. The two that
        # were processed walked all the way through to CURRENT=2.
        self.assertEqual(sorted(_versions(self.repo)), [0, 0, 0, 2, 2])

    def test_zero_or_negative_batch_size_is_short_circuited(self) -> None:
        # Don't pull a candidate set at all; result reports zeros.
        for n in range(1, 4):
            make_pr(self.repo, n)

        for size in (0, -1):
            res = upgrade_schema_versions_task(self.repo.id, batch_size=size, kick_limit=5)
            self.assertEqual(res["considered"], 0)
            self.assertEqual(res["stamped"], 0)
            self.assertEqual(res["kicked"], 0)

        # Nothing got stamped.
        self.assertEqual(_versions(self.repo), [0, 0, 0])

    def test_only_processes_target_repo(self) -> None:
        repo2 = make_repo(owner="o2", name="r2")
        make_pr(self.repo, 1)
        other = make_pr(repo2, 1)

        res = upgrade_schema_versions_task(self.repo.id, batch_size=10, kick_limit=5)

        self.assertEqual(res["considered"], 1)
        self.assertEqual(_versions(self.repo), [2])
        other.refresh_from_db()
        self.assertEqual(other.sync_schema_version, 0)

    def test_kick_budget_caps_kicks(self) -> None:
        # Patch CURRENT=2 in both modules and register a v=2 upgrader that's
        # always incomplete. With kick_limit=2 and 5 PRs at v=1, exactly two
        # kicks are emitted; the rest are deferred to the next pass.
        prs = [make_pr(self.repo, n) for n in range(1, 6)]
        for pr in prs:
            stamp(pr, 1)
        upgrade = _FakeUpgrade(version=2, complete=False)
        register(upgrade)
        with (
            mock.patch.object(sync_schema_upgrades, "CURRENT_SYNC_SCHEMA_VERSION", 2),
            mock.patch("syncer.tasks.upgrade_schema_tasks.CURRENT_SYNC_SCHEMA_VERSION", 2),
        ):
            res = upgrade_schema_versions_task(self.repo.id, batch_size=10, kick_limit=2)

        self.assertEqual(res["considered"], 5)
        self.assertEqual(res["kicked"], 2)
        self.assertEqual(res["kick_budget_remaining"], 0)
        self.assertEqual(upgrade.kick_calls, 2)
        # Nothing got past v=1 (the v=2 work is what was being kicked).
        self.assertEqual(_versions(self.repo), [1, 1, 1, 1, 1])

    def test_auto_stamp_unbounded_alongside_kick_budget(self) -> None:
        # Mix: some PRs at v=0 (need only auto-stamp through v=1, no kick),
        # some PRs at v=1 (need v=2 kick). With kick_limit=1, all v=0 PRs
        # still auto-stamp, but only one v=1 PR gets kicked.
        v0_prs = [make_pr(self.repo, n) for n in (1, 2, 3)]
        v1_prs = [make_pr(self.repo, n) for n in (4, 5)]
        for pr in v1_prs:
            stamp(pr, 1)
        upgrade = _FakeUpgrade(version=2, complete=False)
        register(upgrade)
        with (
            mock.patch.object(sync_schema_upgrades, "CURRENT_SYNC_SCHEMA_VERSION", 2),
            mock.patch("syncer.tasks.upgrade_schema_tasks.CURRENT_SYNC_SCHEMA_VERSION", 2),
        ):
            res = upgrade_schema_versions_task(self.repo.id, batch_size=10, kick_limit=1)

        self.assertEqual(res["considered"], 5)
        self.assertEqual(res["kicked"], 1)
        # The 3 v=0 PRs all auto-stamped to v=1 (orderable by sync_schema_version asc).
        # The 2 v=1 PRs: 1 got kicked (still at v=1), 1 didn't get kicked (no budget) and stays at v=1.
        self.assertEqual(_versions(self.repo), [1, 1, 1, 1, 1])
        # Auto-stamps recorded for the three v=0 PRs.
        self.assertEqual(res["auto_stamped"], 3)
        # is_complete called once per remaining v=1 PR (both still get checked,
        # only the first one with budget gets kicked).
        self.assertEqual(upgrade.kick_calls, 1)
        # Verify only v=0 PRs were auto-stamped (not v=1, which were deliberately stamped).
        for pr in v0_prs:
            pr.refresh_from_db()
            self.assertEqual(pr.sync_schema_version, 1)

    def test_uses_settings_defaults_when_kwargs_missing(self) -> None:
        for n in range(1, 4):
            make_pr(self.repo, n)
        with self.settings(
            SYNCER_SCHEMA_UPGRADE_BATCH_SIZE=2,
            SYNCER_SCHEMA_UPGRADE_KICK_LIMIT=99,
        ):
            res = upgrade_schema_versions_task(self.repo.id)
        self.assertEqual(res["considered"], 2)

    def test_target_version_gate_holds_below_constant(self) -> None:
        # SYNCER_SCHEMA_UPGRADE_TARGET_VERSION=1 with CURRENT=2 means PRs
        # already at v=1 must NOT be selected (the candidate filter clamps to
        # the gate, not the constant). v=0 PRs still auto-stamp through v=1.
        v0_pr = make_pr(self.repo, 1)
        v1_pr = make_pr(self.repo, 2)
        stamp(v1_pr, 1)
        with self.settings(SYNCER_SCHEMA_UPGRADE_TARGET_VERSION=1):
            res = upgrade_schema_versions_task(self.repo.id, batch_size=10, kick_limit=5)
        # Only the v=0 PR is a candidate.
        self.assertEqual(res["considered"], 1)
        self.assertEqual(res["target"], 1)
        self.assertEqual(res["current"], 2)
        v0_pr.refresh_from_db()
        v1_pr.refresh_from_db()
        self.assertEqual(v0_pr.sync_schema_version, 1)
        self.assertEqual(v1_pr.sync_schema_version, 1)

    def test_target_version_gate_above_constant_clamps_safely(self) -> None:
        # A misconfigured gate (5) above CURRENT (2) must clamp to CURRENT.
        pr = make_pr(self.repo, 1)
        with self.settings(SYNCER_SCHEMA_UPGRADE_TARGET_VERSION=5):
            res = upgrade_schema_versions_task(self.repo.id, batch_size=10, kick_limit=5)
        self.assertEqual(res["target"], 2)
        pr.refresh_from_db()
        self.assertEqual(pr.sync_schema_version, 2)


class TestUpgradeSchemaVersionsActiveTask(_RegistryIsolationMixin, TestCase):
    def test_active_fanout_dispatches_per_active_repo(self) -> None:
        repo1 = make_repo(owner="o1", name="r1")
        repo2 = make_repo(owner="o2", name="r2")
        # Inactive repo should not be enqueued.
        make_repo(owner="o3", name="r3", is_active=False)

        with mock.patch("syncer.tasks.upgrade_schema_tasks.upgrade_schema_versions_task.delay") as delay_mock:
            res = upgrade_schema_versions_active_task(batch_size=100, kick_limit=10)

        self.assertEqual(res["repos"], 2)
        self.assertEqual(res["enqueued"], 2)
        self.assertEqual(delay_mock.call_count, 2)
        enqueued_ids = {call.args[0] for call in delay_mock.call_args_list}
        self.assertEqual(enqueued_ids, {repo1.id, repo2.id})
        for call in delay_mock.call_args_list:
            self.assertEqual(call.kwargs, {"batch_size": 100, "kick_limit": 10})
