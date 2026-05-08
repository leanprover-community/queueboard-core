"""Sub-sync units for specific parts of the PR bundle.

Each module exposes pure functions that accept parsed bundle slices and
perform idempotent upserts into the database.

Wire-up invariant for sub-syncs that consume timeline nodes
-----------------------------------------------------------
Any helper here that ingests a sub-collection nested under a
``timelineItems`` node (e.g. ``PullRequestReview.comments``) is invoked
from **three** call sites in :mod:`syncer.services.pr_sync_service`:

1. ``PRSyncService.sync_pull_request_bundle`` — head-of-PR bundle path.
2. The forward ``client.get_timeline_page`` loop in
   ``PRSyncService.sync_pull_request`` — newer-than-bundle pages.
3. The backward ``client.get_timeline_page_back`` loop in the same
   method — historical pages, the path the schema-upgrade waves
   (``UpgradeToV*.kick``) drive.

If a new sub-sync is wired into only one or two of those paths, rewalks
silently drop data that's already on the wire. This actually happened
in the v=2 wave (design doc 044 §Chunk 5b) and required a v=3 recovery
wave to fix. Before adding a new sub-sync, verify::

    grep -n 'sync_timeline_events(' qb_site/syncer/services/pr_sync_service.py

returns three call sites and add the new sub-sync invocation next to
each one (with a corresponding regression test per call site).

See ``qb_site/syncer/AGENTS.md`` ("Timeline ingest invariants" and
"Checklist for new ingestion code") for the full rationale.
"""
