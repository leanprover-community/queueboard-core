# Commit Backfill Semantics and Resets

## Context
- Syncer persists per-PR commit backfill state on `syncer.PullRequest` via:
  - `commits_backfill_cursor`
  - `commits_backfill_done`
  - `commits_earliest_synced_at`
- Initial implementation mirrored timeline backfill and directly mapped GraphQL `commits.pageInfo.hasPreviousPage` to `commits_backfill_done` on every page.
  - `commits_backfill_done` could regress from `True` back to `False` when later pages reported `hasPreviousPage=True`, even if we had already walked the full commits connection at least once.
  - Analyzer gates revision-based CI backfill on `commits_backfill_done`, so regressions made it difficult to reason about when the commit history was “complete enough”.
- `HEAD_FORCE_PUSHED` events (stored as `PRTimelineEvent(type=HEAD_FORCE_PUSHED)`) represent head changes that should invalidate any previous completeness guarantee for commit backfill.

## Decision
- Make `commits_backfill_done` monotone for a given head lineage:
  - Only transition from `False` → `True` when a commit page is processed where `commits.pageInfo.hasPreviousPage == False`.
  - Never flip `commits_backfill_done` from `True` back to `False` based solely on `hasPreviousPage`.
  - Skip commit backfill paging entirely when `commits_backfill_done` is already `True`.
- Reset commit backfill state when a new force-push event is ingested:
  - `syncer.services.sub.timeline_sync.sync_timeline_events` sets:
    - `commits_backfill_done = False`
    - `commits_backfill_cursor = None`
    - `commits_earliest_synced_at = None`
  - Reset happens only when a new `HEAD_FORCE_PUSHED` event row is created for the PR (idempotent on repeated syncs).
- Analyzer treats commit backfill as safe to rely on only when:
  - `timeline_backfill_done == True` (complete force-push history in `PRTimelineEvent`), and
  - `commits_backfill_done == True` (commits connection has been walked to its oldest page since the last force-push reset).

## Consequences
- `commits_backfill_done` now has clear, monotone semantics:
  - For a given head lineage, once complete commit backfill is achieved, it remains `True` until explicitly reset by a new `HEAD_FORCE_PUSHED` event.
  - Up-to-date sync runs (`syncer.sync_pr` skip path) can safely spend commit backfill budget without risking regressions of the flag.
- Force-pushes explicitly invalidate commit backfill:
  - Any new `HEAD_FORCE_PUSHED` event clears the commit backfill state, ensuring Analyzer does not treat historical commit coverage as valid for the new head lineage.
  - `commits_earliest_synced_at` is also cleared, so admin views no longer display a misleading earliest timestamp after a head rewrite.
- Timeline vs commits interaction:
  - While `timeline_backfill_done == False`, we may not yet know the full set of historical force-pushes; `commits_backfill_done` is therefore not used as a correctness signal in Analyzer until timeline backfill is complete.
  - Once `timeline_backfill_done` becomes `True`, any `HEAD_FORCE_PUSHED` events discovered along the way will have reset commit backfill at least once, so a subsequent `commits_backfill_done == True` reflects “complete since the last known force-push”.

## Operational Notes
- Code changes:
  - `qb_site/syncer/services/pr_sync_service.py`
    - Commit paging (`max_commit_pages`) now guards on `not pr_obj.commits_backfill_done` and only sets `commits_backfill_done = True` when processing a page with `hasPreviousPage == False`.
    - Commit backfill paging (`backfill_commit_pages`) keeps `commits_backfill_done` monotone with the same rule.
  - `qb_site/syncer/tasks/sync_tasks.py`
    - Up-to-date path (`sync_pr_task` skip branch) skips commit backfill when `commits_backfill_done` is already `True` and uses monotone updates when it runs.
  - `qb_site/syncer/services/sub/timeline_sync.py`
    - `sync_timeline_events` tracks whether any new `HEAD_FORCE_PUSHED` event was created; if so, it resets the commit backfill fields on the PR.
- Settings and admin:
  - No new settings are required; behavior is driven by existing `SYNCER_COMMITS_BACKFILL_PAGES` and `SYNCER_RATE_REMAINING_MIN`.
  - PR admin continues to surface `timeline_backfill_done` and `commits_backfill_done`; after this change, those flags are more stable and meaningful for operators.

## Alternatives (Optional)
- Head SHA / epoch-based tracking (not chosen here):
  - Store the current head SHA and a `commits_backfill_epoch` on `syncer.PullRequest`, incrementing the epoch when the head SHA changes and tying `commits_backfill_done` to a specific epoch.
  - Pros: tighter coupling to the actual head SHA; force-push detection could rely on header state even if timeline events are missing.
  - Cons: requires schema and GraphQL query changes; more moving parts for V1.1.
- Minimal monotone flag without resets:
  - Only make `commits_backfill_done` monotone (never `True` → `False`) and do not reset on force-push.
  - Pros: smallest code change.
  - Cons: `commits_backfill_done` could remain `True` across head rewrites, weakening its value as a correctness gate for Analyzer backfills.

