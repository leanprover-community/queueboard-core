"""v3 schema upgrader: re-walk timeline under the fixed page paths.

The v2 wave (Chunk 5) shipped with a wire-up gap: the forward and
backward timeline-page loops in :class:`PRSyncService` persisted
``REVIEW_*`` and ``ISSUE_COMMENTED`` ``PRTimelineEvent`` rows but did
not invoke the inline-comments sub-sync, so any inline review comments
on reviews that surfaced via those pages were silently dropped.
``PRReviewInlineCommentBackfill`` rows for >K-comment reviews were
likewise never created. PRs whose v=2 walk completed before the fix
sit at ``sync_schema_version=2`` with missing
``PRReviewInlineComment`` rows.

v3 fixes those PRs by re-running the same rewalk under the corrected
page paths. The actual fix is in :class:`PRSyncService` (it now calls
the inline-comments service on every timeline page, not just the
bundle); this upgrader is mechanically identical to
:class:`UpgradeToV2` and just re-triggers the rewalk.

Pairs with migration ``0045_reset_timeline_backfill_for_v3_wave``
which resets ``timeline_backfill_done=False`` /
``timeline_backfill_cursor=NULL`` for every PR with
``sync_schema_version<3`` (option (a) from design doc 044 §Chunk 5,
applied a second time). PRs already at v=2 have ``timeline_backfill_done=True``
from the v2 wave; without the reset, ``UpgradeToV3.is_complete``
would short-circuit to True and the dispatcher would auto-stamp them
to v=3 without a rewalk — repeating the v2-era pitfall.

See ``docs/design-decisions/044-sync-schema-versioning-and-comment-review-timeline-events.md``
§Chunk 5b for the full rationale.
"""

from __future__ import annotations

from syncer.services.sync_schema_upgrade_v2 import UpgradeToV2


class UpgradeToV3(UpgradeToV2):
    """Advance a PR from v=2 to v=3 by re-walking its timeline under fixed paths.

    Inherits :meth:`is_complete` and :meth:`kick` unchanged from
    :class:`UpgradeToV2` — both upgraders ride the same
    ``timeline_backfill_done`` flag, paired with a deploy-time reset
    migration. Only the ``version`` discriminator differs.
    """

    version: int = 3


def register_v3_upgrader() -> None:
    """Idempotently register :class:`UpgradeToV3` with the registry.

    Called from ``SyncerConfig.ready()`` and safe to call multiple times
    (re-registration is a no-op).
    """
    from syncer.services.sync_schema_upgrades import _REGISTRY, register

    if _REGISTRY.get(UpgradeToV3.version) is not None:
        return
    register(UpgradeToV3())
