# Timeline Backfill Done Flag With Filtered Timeline Windows

## Context
- Syncer fetches PR timeline items via the PR bundle using `timelineSince` derived from `last_synced_at`.
- `timeline_backfill_done` is seeded from the bundle pageInfo (`hasPreviousPage`).
- GraphQL `hasPreviousPage` reflects the filtered window, not the full timeline.
- This can mark `timeline_backfill_done=True` even when older events are missing, which blocks the incomplete‑PR backfill task from repairing history.

### Example (current behavior)
- PR timeline events: E1 (Jan 01), E2 (Jan 10), E3 (Feb 01), E4 (Mar 01).
- PR already exists with `last_synced_at=Feb 15`; sync runs with `timelineSince=Feb 15 - epsilon`.
- Bundle returns only E4; `pageInfo.hasPreviousPage=False` because the filtered window has no older items.
- We set `timeline_backfill_done=True`, even though E1–E3 were never fetched.
- Incomplete‑PR backfill skips the PR because the done flag is true, so history stays incomplete.

## Decision
- Only set `timeline_backfill_done` from bundle pageInfo when the bundle is unfiltered
  (i.e., `timelineSince` is not provided).
- When `timelineSince` is used, still seed `timeline_backfill_cursor`, but do not flip the done flag.

## Consequences
- The done flag now implies we actually reached the beginning via backfill paging.
- Some PRs will remain “incomplete” until backfill runs, increasing the number of candidates for
  incomplete‑PR backfill tasks.
- Historical completeness is improved without changing the incremental sync window.

## Operational Notes
- No migrations required.
- Existing PRs already marked `timeline_backfill_done=True` remain unchanged; use a manual or
  one‑time repair process to reset flags for known bad PRs if needed.

## Alternatives
- Add a second unfiltered query for state‑change events (close/reopen/draft) to improve correctness
  without full backfill (rejected for now due to extra per‑sync query cost).
