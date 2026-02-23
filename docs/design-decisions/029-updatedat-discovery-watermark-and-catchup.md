# UpdatedAt Discovery Watermark and Catch-up Continuations

## Context
- Repository discovery previously depended on a sliding `updatedAt` lookback window (`SYNCER_DISCOVERY_LOOKBACK_MINUTES`).
- During outages or prolonged queue starvation longer than that window, updates to already-known PRs could be missed permanently.
- Existing createdAt history backfill (`RepoBackfillCursor`) ensures first-seen coverage, but does not recover missed `updatedAt` discovery updates.

## Decision
- Introduce a per-repo `updatedAt` discovery state machine with:
  - a durable watermark for fully scanned cutoffs,
  - resumable continuation cursor/cutoff state for incomplete scans.
- Discovery now runs in explicit modes:
  - fresh sweep (sliding cutoff + overlap),
  - continuation (fixed cutoff + persisted cursor).
- Watermark advances only on completed scan coverage to the cutoff.
- Incomplete scans always persist continuation state and are resumed.

## Architecture

### State Model
- New model: `syncer.RepoDiscoveryState` (one-to-one with `core.Repository`).
- Fields:
  - `last_successful_cutoff_at`: fully scanned cutoff watermark.
  - `continuation_cutoff_at`: fixed cutoff for in-progress catch-up sequence.
  - `continuation_cursor`: GraphQL cursor to resume from.
  - `continuation_started_at`: when current continuation sequence began.
  - `last_attempted_at`: last discovery attempt timestamp.
  - `last_successful_at`: last successful full-scan timestamp.
- Helper transitions:
  - `mark_attempted()`
  - `set_continuation(cutoff_at, cursor)`
  - `mark_success(cutoff_at)` (also clears continuation fields).

### Discovery Query Contract
- `GitHubClient.discover_changed_pr_numbers(...)` provides structured progress:
  - `numbers`
  - `next_cursor`
  - `reached_cutoff`
  - `hit_limit`
- Supports continuation input via `after=...`.
- `get_changed_pr_numbers(...)` remains as compatibility wrapper returning only `numbers`.
- Pagination uses `first=min(per_page, remaining)` so a run does not stop mid-page; this keeps continuation safe when resuming from page `endCursor`.

### Repo Discovery Task State Machine
- Task: `syncer.sync_repo_since`.
- Steps under per-repo advisory lock:
  1. Load/create `RepoDiscoveryState`; mark attempt.
  2. Choose mode:
     - continuation when both continuation cutoff and cursor exist,
     - otherwise fresh.
  3. Compute effective cutoff:
     - fresh: base lookback cutoff, optionally pulled older via watermark overlap,
     - continuation: fixed persisted continuation cutoff.
  4. Run structured discovery.
  5. Persist state transition:
     - complete scan (`reached_cutoff` or no `next_cursor`): `mark_success`,
     - incomplete scan: `set_continuation`.
  6. Enqueue `sync_pr` tasks under existing dynamic batch/rate budget rules.
  7. Schedule continuation when needed.

### Continuation Scheduling
- Low-budget path (existing behavior retained):
  - if remaining GitHub budget is low, defer to `resetAt` with debounce.
- Cap/page exhaustion path (new):
  - if scan incomplete even with healthy budget, schedule near-term continuation.
- Both paths use `debounce_repo_schedule(...)` to suppress duplicate scheduling.
- Continuation reasons are surfaced in task result (`low_budget` / `cap_exhausted`).

### Failure Handling
- If continuation discovery fails (for example stale/invalid cursor):
  - clear continuation cursor/cutoff/start,
  - retry immediately in fresh recovery mode using overlap logic.
- This fail-safe prefers duplicate work over missed updates.

## Invariants
- Watermark invariant:
  - `last_successful_cutoff_at` represents fully scanned coverage only.
  - It must not advance on partial discovery progress.
- Continuation invariant:
  - continuation cutoff is fixed for a continuation sequence.
- Serialization invariant:
  - state transitions occur under per-repo advisory lock.
- Idempotency invariant:
  - duplicate PR enqueue is acceptable; downstream sync remains idempotent.

## Observability
- `SyncerConvergenceSnapshot` includes discovery diagnostics:
  - `discovery_lag_seconds`
  - `discovery_continuation_active`
  - `discovery_last_attempted_at`
  - `discovery_last_successful_at`
- Admin surfaces:
  - read-only `RepoDiscoveryState` admin
  - expanded convergence snapshot admin columns/filters for discovery status.
- Task result diagnostics include:
  - mode, scan completion, cutoff/cursor progress, continuation scheduling reason.

## Settings
- Added:
  - `SYNCER_DISCOVERY_OVERLAP_SECONDS` (default `300`)
  - `SYNCER_DISCOVERY_CONTINUATION_DELAY_SECONDS` (default `5`)
- Existing settings still used:
  - `SYNCER_DISCOVERY_LOOKBACK_MINUTES`
  - `SYNCER_DISCOVERY_LIMIT`
  - `SYNCER_REPO_ENQUEUE_BATCH_MAX`
  - `SYNCER_RATE_REMAINING_MIN`

## Operational Notes
- Rollout does not require full-repo re-scan:
  - `RepoDiscoveryState` is lazy-created per repo on first discovery run.
- This change prevents future misses from outages/cap pressure, but does not retroactively recover updates already missed before rollout.
  - For one-time deeper catch-up, use existing `sync_repo --since ...` workflows.
- Schema changes:
  - `syncer` migrations `0028` (discovery state) and `0029` (convergence observability).

## Consequences
- Pros:
  - robust recovery from long outages and high update churn,
  - explicit progress semantics for discovery coverage,
  - improved operational visibility for lag and continuation health.
- Trade-offs:
  - more state complexity in repo sync task,
  - intentional duplicate work in overlap/recovery paths to preserve correctness.

## Out of Scope
- Queue isolation / dedicated GitHub queue strategy changes.
- Analyzer scheduling redesign.
- CreatedAt history backfill policy changes outside updatedAt discovery flow.

## References
- `qb_site/syncer/models/repo_discovery_state.py`
- `qb_site/syncer/services/github_client.py`
- `qb_site/syncer/tasks/sync_tasks.py`
- `qb_site/syncer/tasks/collect_convergence.py`
- `qb_site/syncer/models/convergence_snapshot.py`
- `qb_site/syncer/admin.py`
- `qb_site/qb_site/settings/base.py`
