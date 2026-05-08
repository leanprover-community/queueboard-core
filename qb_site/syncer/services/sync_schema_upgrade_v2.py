"""v2 schema upgrader: re-walk timeline under the v2 fragments.

The v2 ingestion expansion captures `IssueComment` /
`PullRequestReview` / `ReviewDismissedEvent` / `ReviewRequestedEvent` /
`ReviewRequestRemovedEvent` `timelineItems` plus nested
`PullRequestReview.comments` (inline review comments). Fresh syncs
after the Chunk 4b/4c deploy already capture these for the head of the
timeline; this upgrader handles already-fully-walked PRs whose
``timeline_backfill_done`` flag was set by a v1-era walk that didn't
request the new ``itemTypes``.

Correctness coupling
--------------------
The check ``is_complete(pr) := pr.timeline_backfill_done`` is correct
**only** when the migration shipping with this upgrader has reset
``timeline_backfill_done=False`` for every PR with
``sync_schema_version < 2`` (option (a) in design doc 044 §Chunk 5).
Without that reset, v1-era walks would short-circuit ``is_complete`` to
True at v=2 and the dispatcher would auto-stamp the PR without ever
re-walking history under the v2 fragments. The data migration
``0044_reset_timeline_backfill_for_v2_wave`` provides that guarantee;
do not call this upgrader without that migration applied.

See ``docs/design-decisions/044-sync-schema-versioning-and-comment-review-timeline-events.md``
§Chunk 5 for the full rationale.
"""

from __future__ import annotations

import logging

from django.conf import settings

from syncer.models import PullRequest


logger = logging.getLogger(__name__)


class UpgradeToV2:
    """Advance a PR from v=1 to v=2 by re-walking its timeline."""

    version: int = 2

    def is_complete(self, pr: PullRequest) -> bool:
        # Paired with the migration that resets timeline_backfill_done=False
        # for every PR at sync_schema_version<2. Under that pairing, a True
        # observation here means the post-deploy rewalk has run.
        return bool(pr.timeline_backfill_done)

    def kick(self, pr: PullRequest) -> None:
        # Reset the backfill flags so backfill_repo_incomplete_prs picks the
        # PR up on subsequent passes, and so this kick's sync_pr_task call
        # itself does meaningful rewalk work rather than just a head sync.
        # Use a guarded UPDATE to avoid clobbering concurrent writes.
        PullRequest.objects.filter(pk=pr.pk).update(
            timeline_backfill_done=False,
            timeline_backfill_cursor=None,
        )
        # Mirror the in-memory copy so callers reading pr.* downstream see
        # the reset state.
        pr.timeline_backfill_done = False
        pr.timeline_backfill_cursor = None

        # Local import to avoid circular import at module load: the tasks
        # module imports from services, not the other way around.
        from syncer.tasks.sync_tasks import sync_pr_task

        backfill_pages = int(getattr(settings, "SYNCER_TIMELINE_BACKFILL_PAGES", 2))
        sync_pr_task.delay(
            pr.repository_id,
            int(pr.number),
            force=True,
            backfill_timeline_pages=backfill_pages,
        )
        logger.info(
            "sync_schema_upgrade_v2.kick pr_id=%s repo_id=%s number=%s backfill_pages=%s",
            pr.pk,
            pr.repository_id,
            pr.number,
            backfill_pages,
        )


def register_v2_upgrader() -> None:
    """Idempotently register :class:`UpgradeToV2` with the registry.

    Called from ``SyncerConfig.ready()`` and safe to call multiple times
    (re-registration is a no-op).
    """
    # Local import keeps this module importable for tests that don't want
    # to drag the registry in.
    from syncer.services.sync_schema_upgrades import _REGISTRY, register

    if _REGISTRY.get(UpgradeToV2.version) is not None:
        return
    register(UpgradeToV2())
