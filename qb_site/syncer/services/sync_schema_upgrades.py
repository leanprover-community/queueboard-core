"""Sync schema versioning + upgrader registry.

The upgrader registry is the *only* place that advances
``PullRequest.sync_schema_version``. ``PRSyncService`` does not write that
column; this avoids prematurely stamping a PR to a higher version when its
sync completes without the version's upgrader having actually run.

Adding a new "we want to capture X" expansion follows this pattern:

1. Add the new ingestion path (model fields, GraphQL, normalizer).
2. Implement an upgrader (see ``SchemaUpgrade`` below) for the next version
   number that returns ``is_complete(pr)=True`` only when the new data has
   been captured for that PR, and whose ``kick(pr)`` enqueues whatever work
   is required to capture it.
3. Register the upgrader against its target version with :func:`register`.
4. Bump :data:`CURRENT_SYNC_SCHEMA_VERSION`.

If a version has no associated upgrader (e.g. v=1, where the data v=1 tracks
is already written on every ``PRSyncService`` sync), the dispatcher
auto-stamps and continues. See ``docs/design-decisions/044-...md`` for the
rationale and trade-offs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from syncer.models import PullRequest


logger = logging.getLogger(__name__)


CURRENT_SYNC_SCHEMA_VERSION: int = 2
"""Current target value of ``PullRequest.sync_schema_version``.

Bumped each time a new ingestion expansion is rolled out together with its
upgrader. The dispatcher selects PRs where
``sync_schema_version < CURRENT_SYNC_SCHEMA_VERSION`` and walks the registry
from ``pr.sync_schema_version + 1`` up to this value.

A staging deploy can clamp this via the ``SYNCER_SCHEMA_UPGRADE_TARGET_VERSION``
setting (see :func:`effective_target_version`); production typically leaves
the gate equal to this constant.
"""


def effective_target_version() -> int:
    """Return the target version honored by the dispatcher loop.

    Resolved as ``min(SYNCER_SCHEMA_UPGRADE_TARGET_VERSION,
    CURRENT_SYNC_SCHEMA_VERSION)`` if the setting is present and non-None;
    otherwise just :data:`CURRENT_SYNC_SCHEMA_VERSION`. Clamping to the
    constant keeps a misconfigured setting from advancing PRs past versions
    that have no registered upgrader.
    """
    # Local import: keep the module importable in test contexts that build
    # a minimal Django setup.
    from django.conf import settings

    raw = getattr(settings, "SYNCER_SCHEMA_UPGRADE_TARGET_VERSION", None)
    if raw is None:
        return int(CURRENT_SYNC_SCHEMA_VERSION)
    return min(int(raw), int(CURRENT_SYNC_SCHEMA_VERSION))


class SchemaUpgrade(Protocol):
    """One step in the upgrade chain, advancing a PR to ``version``."""

    version: int

    def is_complete(self, pr: PullRequest) -> bool:
        """Return True iff the data this version captures is present for ``pr``."""
        ...

    def kick(self, pr: PullRequest) -> None:
        """Enqueue whatever work is needed to make ``is_complete`` eventually True."""
        ...


_REGISTRY: dict[int, SchemaUpgrade] = {}


def register(upgrade: SchemaUpgrade) -> SchemaUpgrade:
    """Register ``upgrade`` against its declared version. Returns it (for use as a decorator)."""
    if upgrade.version in _REGISTRY:
        raise ValueError(
            f"SchemaUpgrade for version {upgrade.version} already registered "
            f"({_REGISTRY[upgrade.version]!r}); cannot register {upgrade!r}"
        )
    if upgrade.version < 1:
        raise ValueError(f"SchemaUpgrade.version must be >= 1, got {upgrade.version!r}")
    _REGISTRY[upgrade.version] = upgrade
    return upgrade


def get_registered(version: int) -> SchemaUpgrade | None:
    """Return the upgrader registered at ``version``, or None if unregistered."""
    return _REGISTRY.get(int(version))


def stamp(pr: PullRequest, version: int) -> bool:
    """Advance ``pr.sync_schema_version`` to ``version``.

    Uses a guarded UPDATE so concurrent dispatchers cannot walk the column
    backward. Returns True iff a row was actually advanced (i.e. the in-memory
    ``pr`` was below ``version``); the in-memory instance is updated to match
    on success so the caller's loop reads the new value.
    """
    updated = PullRequest.objects.filter(pk=pr.pk, sync_schema_version__lt=int(version)).update(sync_schema_version=int(version))
    if updated:
        pr.sync_schema_version = int(version)
        return True
    return False


@dataclass
class DispatchOutcome:
    """Per-PR result of one dispatcher pass."""

    stamped_to: int | None  # final sync_schema_version after this pass, or None if unchanged
    kicked: bool  # True iff dispatcher emitted a kick during this pass
    auto_stamped_versions: tuple[int, ...]  # versions auto-stamped due to missing upgrader


def dispatch(pr: PullRequest, *, kick_budget: int = 1, target_version: int | None = None) -> DispatchOutcome:
    """Walk one PR through ``pr.sync_schema_version + 1 ... target``.

    For each step ``s``:

    - If no upgrader is registered for ``s``: auto-stamp and continue.
    - If an upgrader is registered and ``is_complete(pr)`` returns True:
      stamp and continue.
    - Otherwise: if ``kick_budget > 0``, call ``kick(pr)`` and stop. If the
      caller has run out of kick budget, stop without kicking; the next pass
      tries again.

    ``target_version`` lets a caller (typically the periodic task) gate the
    walk below :data:`CURRENT_SYNC_SCHEMA_VERSION`. ``None`` defaults to the
    constant. The value is clamped to the constant so a misconfigured gate
    cannot advance PRs past versions with no registered upgrader.

    The dispatcher modifies ``pr`` in-place (only ``sync_schema_version``).
    """
    if kick_budget < 0:
        raise ValueError(f"kick_budget must be >= 0, got {kick_budget!r}")

    initial = int(pr.sync_schema_version)
    if target_version is None:
        target = int(CURRENT_SYNC_SCHEMA_VERSION)
    else:
        target = min(int(target_version), int(CURRENT_SYNC_SCHEMA_VERSION))
    auto_stamped: list[int] = []
    kicked = False
    final_version: int | None = None

    for step in range(initial + 1, target + 1):
        upgrade = _REGISTRY.get(step)
        if upgrade is None:
            if stamp(pr, step):
                auto_stamped.append(step)
                final_version = step
                logger.info(
                    "sync_schema_upgrades.auto_stamp pr_id=%s repo_id=%s number=%s version=%s",
                    pr.pk,
                    pr.repository_id,
                    pr.number,
                    step,
                )
            continue

        if upgrade.is_complete(pr):
            if stamp(pr, step):
                final_version = step
            continue

        if kick_budget <= 0:
            break

        upgrade.kick(pr)
        kicked = True
        break

    return DispatchOutcome(
        stamped_to=final_version,
        kicked=kicked,
        auto_stamped_versions=tuple(auto_stamped),
    )
