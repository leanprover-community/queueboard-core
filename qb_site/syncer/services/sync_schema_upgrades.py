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
3. Register the upgrader against its target version.
4. Bump :data:`CURRENT_SYNC_SCHEMA_VERSION`.

The full registry/dispatcher and per-version upgraders land in subsequent
chunks of design doc 044; this module currently only declares the constant
and the protocol so other modules can import them.
"""

from __future__ import annotations

from typing import Protocol

from syncer.models import PullRequest


CURRENT_SYNC_SCHEMA_VERSION: int = 1
"""Current target value of ``PullRequest.sync_schema_version``.

Bumped each time a new ingestion expansion is rolled out together with its
upgrader. The dispatcher selects PRs where
``sync_schema_version < CURRENT_SYNC_SCHEMA_VERSION`` and walks the registry
from ``pr.sync_schema_version + 1`` up to this value.
"""


class SchemaUpgrade(Protocol):
    """One step in the upgrade chain, advancing a PR to ``version``."""

    version: int

    def is_complete(self, pr: PullRequest) -> bool:
        """Return True iff the data this version captures is present for ``pr``."""
        ...

    def kick(self, pr: PullRequest) -> None:
        """Enqueue whatever work is needed to make ``is_complete`` eventually True."""
        ...
