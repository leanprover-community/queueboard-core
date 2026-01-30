# Reviewer Opt-Outs and Timeline Assignment Events

## Context
- Reviewers sometimes unassign themselves from PRs, but auto-assignment may re-suggest them without remembering the opt-out.
- The auto-assignment logic lives in `qb_site/analyzer/services/reviewer_assignment.py` and operates on queue snapshots.
- We now ingest assignment/unassignment timeline events from GitHub to capture the precise assignment history (even when assignee lists are stale or missed due to sync timing).
- We need a strategy that is correct under re-syncs, partial timelines, and backfill runs, without creating opt-outs from historical/unrelated events.

## Decision
- Add a persistent reviewer opt-out table and drive its state from assignment/unassignment timeline events.
  - Model: `analyzer.ReviewerOptOut` keyed by `(repository, pr_number, reviewer_login)` with `active`, `opted_out_at`, and `cleared_at`.
  - Used by `analyzer/services/reviewer_assignment.py` to exclude opted-out reviewers from auto-assignments for that PR.
- Store assignment event progress on the PR to make processing **monotonic and deterministic**.
  - New field: `syncer.PullRequest.last_assignment_event_at`.
  - When processing a batch of timeline events, ignore assignment/unassignment events with `occurred_at <= last_assignment_event_at`.
  - Apply newer events in chronological order, then advance `last_assignment_event_at` to the max processed timestamp.
- Populate assignment/unassignment events via timeline items rather than assignee diffing.
  - Timeline event types: `ASSIGNED_EVENT` and `UNASSIGNED_EVENT`.
  - Store `actor_login` and `assignee_login` on `syncer.PRTimelineEvent` for future analytics.
- Backfills of **older** timeline pages must **not** mutate opt-out state.
  - Backfill flows call `sync_timeline_events` only (no opt-out processing).
  - Opt-outs are applied only in the main PR sync path that uses the bundle timeline (and optional forward paging).

## Consequences
- Correctness improves: assignment/unassignment is captured even when assignee lists change faster than the sync cadence.
- Monotonic processing prevents older events (replays/backfills) from overriding newer opt-outs.
- Backfills can safely re-fetch history to fill `actor_login` / `assignee_login` without corrupting opt-out state.
- If a newer assignment/unassignment event is never fetched (e.g., missing sync), opt-out state can be stale; this is a data freshness issue, not an ordering/correctness issue.

## Operational Notes
- Schema/migrations:
  - Add `ReviewerOptOut` in `qb_site/analyzer/models/reviewer_opt_out.py`.
  - Add `last_assignment_event_at` to `syncer.PullRequest`.
  - Run `uv run python qb_site/manage.py makemigrations analyzer syncer` and `uv run python qb_site/manage.py migrate`.
- Timeline ingestion:
  - Update GraphQL timeline queries to include `ASSIGNED_EVENT`/`UNASSIGNED_EVENT` and `actor { login }`.
  - `syncer/services/sub/timeline_sync.py` updates missing fields on existing timeline events.
- Opt-out processing:
  - `PRSyncService._apply_assignment_opt_outs` applies assignment events in chronological order, ignores events at or before `last_assignment_event_at`, then advances the PR marker.
- Backfill refresh:
  - To repopulate actor/assignee data for existing events, reset timeline backfill state (scope as needed):
    - `timeline_backfill_done = false`
    - `timeline_backfill_cursor = NULL`
    - `timeline_earliest_synced_at = NULL` (optional)
  - Then run the existing backfill tasks (e.g., `syncer.backfill_repo_incomplete_prs`) until completion.
  - Backfill pages (`timeline_page_back`) do **not** apply opt-outs by design.

## Alternatives (Optional)
- Assignee diffing only: simpler but misses rapid assign/unassign windows and provides no actor attribution.
- Recompute opt-outs from full timeline history each time: strongest correctness but expensive; can be reserved for a repair command if needed.
- Store per-reviewer timestamps on opt-out rows: insufficient to guard against out-of-order events across multiple reviewers on the same PR.
