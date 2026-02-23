# UpdatedAt Discovery Watermark and Catch-up Continuations

## Context
- The sync pipeline currently relies on a sliding discovery window in `syncer.sync_repo_since` (`SYNCER_DISCOVERY_LOOKBACK_MINUTES`, default 60).
- Discovery is additionally bounded by per-run caps (`SYNCER_DISCOVERY_LIMIT`, `SYNCER_REPO_ENQUEUE_BATCH_MAX`) and rate budget guards.
- During outages or prolonged task starvation (> lookback window), PR updates can be missed permanently because no durable updatedAt progress marker is persisted.
- Existing `RepoBackfillCursor` is createdAt-based and guarantees eventual first-seen coverage of PRs, but does not guarantee recovery of updates to already-known PRs.
- We want to close this outage gap first. We will defer queue-isolation changes and any broader scheduling refactor.

## Problem Statement
- We need discovery semantics that are robust to:
  - sync task downtime longer than the lookback window,
  - high update churn beyond per-run discovery caps,
  - low-budget deferrals and partial progress.
- We must preserve idempotent behavior and avoid dropping updates when tasks partially succeed or retry.

## Decision
- Introduce a per-repo **updatedAt discovery state machine** with:
  - a durable watermark of the last fully scanned cutoff,
  - resumable continuation cursor state for in-progress catch-up scans.
- Extend repo discovery to run in two explicit modes:
  - **fresh sweep mode** (new sliding-window scan),
  - **continuation mode** (resume unfinished scan with fixed cutoff).
- Advance the watermark only after a full scan to cutoff has completed successfully.
- Keep overlap between scans to protect against boundary and race conditions.

## Scope of Immediate Work
- In scope:
  - new persistence model for updatedAt discovery progress,
  - resumable discovery pagination and continuation scheduling,
  - task semantics for safe watermark advancement,
  - tests for outage and high-churn recovery behavior,
  - observability for discovery lag and continuation state.
- Out of scope for this phase:
  - queue isolation (`SYNCER_GITHUB_QUEUE` + dedicated worker),
  - analyzer scheduling/prioritization redesign,
  - major backfill policy changes outside updatedAt discovery flow.

## Design

### 1) New Model: `RepoDiscoveryState`
- Add a new one-to-one model in `syncer.models` keyed by `Repository`.
- Proposed fields:
  - `repository` (OneToOne, required).
  - `last_successful_cutoff_at` (`DateTimeField`, nullable):
    - the oldest boundary for which updatedAt discovery has been fully scanned.
  - `continuation_cutoff_at` (`DateTimeField`, nullable):
    - fixed cutoff for the currently active continuation run.
  - `continuation_cursor` (`TextField`, nullable):
    - GraphQL cursor for `pullRequests(orderBy: UPDATED_AT DESC, after=...)`.
  - `continuation_started_at` (`DateTimeField`, nullable):
    - when current continuation sequence began.
  - `last_attempted_at` (`DateTimeField`, nullable):
    - most recent attempt timestamp (for diagnostics).
  - `last_successful_at` (`DateTimeField`, nullable):
    - most recent successful completion timestamp.
- Notes:
  - This is intentionally separate from `RepoBackfillCursor` because state/order/semantics differ (updatedAt DESC recovery vs createdAt ASC history walk).

### 2) Discovery Query Contract
- Extend `GitHubClient.get_changed_pr_numbers(...)` or add a sibling API that:
  - accepts `after` cursor input,
  - enforces per-call/per-run cap,
  - returns structured progress output:
    - `numbers`,
    - `next_cursor`,
    - `reached_cutoff` (encountered `updatedAt < cutoff`),
    - `hit_limit` (local cap exhausted before completion).
- Query ordering remains:
  - `pullRequests(states: ..., orderBy: {field: UPDATED_AT, direction: DESC})`.

### 3) Task State Machine in `sync_repo_since_task`
- Step A: acquire repo advisory lock (existing behavior).
- Step B: load/create `RepoDiscoveryState`.
- Step C: choose mode.
  - Continuation mode if `continuation_cursor` and `continuation_cutoff_at` exist.
  - Fresh mode otherwise.
- Step D: execute discovery page(s), enqueue `sync_pr` tasks as budget allows.
- Step E: persist state transition:
  - If full scan to cutoff completes:
    - set `last_successful_cutoff_at = effective_cutoff`,
    - set `last_successful_at = now`,
    - clear continuation fields.
  - If scan incomplete:
    - persist `continuation_cutoff_at` and `continuation_cursor`,
    - preserve watermark,
    - schedule continuation.

### 4) Effective Cutoff and Overlap
- Fresh mode computes:
  - `base_cutoff = now - SYNCER_DISCOVERY_LOOKBACK_MINUTES`.
  - `overlap_seconds` from new setting (for example 300-600 seconds).
- If watermark exists:
  - `effective_cutoff = min(base_cutoff, last_successful_cutoff_at - overlap)`.
- Rationale:
  - overlap tolerates timestamp equality/races and keeps discovery idempotent.

### 5) Watermark Advancement Rule (Critical Invariant)
- Only advance `last_successful_cutoff_at` when the scan for that cutoff is complete.
- Never advance on:
  - low-budget defer before completion,
  - partial enqueue/processing,
  - exceptions/retries after partial progress.
- Consequence:
  - recovery may duplicate work, but should not lose updates.

### 6) Continuation Scheduling
- Keep existing rate-budget defer behavior (`resetAt`) and debounce strategy.
- Continuation tasks must resume from persisted cursor/cutoff rather than recomputing a fresh sliding cutoff.
- Continuation should also be scheduled when local cap is reached before cutoff completion, not only on rate-low events.

### 7) Compatibility with Existing Backfills
- `RepoBackfillCursor` and createdAt history backfill remain unchanged.
- Incomplete/pending-CI/engagement backfills remain unchanged.
- This change addresses the specific gap where remote updates are missed because updatedAt discovery window was not fully traversed.

## Settings Plan
- Add:
  - `SYNCER_DISCOVERY_OVERLAP_SECONDS` (default 300).
  - optional `SYNCER_DISCOVERY_CONTINUATION_MAX_PAGES` (if we want explicit per-run page budget).
- Keep existing:
  - `SYNCER_DISCOVERY_LOOKBACK_MINUTES`,
  - `SYNCER_DISCOVERY_LIMIT`,
  - `SYNCER_REPO_ENQUEUE_BATCH_MAX`,
  - `SYNCER_RATE_REMAINING_MIN`.

## Invariants and Subtleties
- Invariant 1:
  - Watermark represents fully completed scan boundary, never partial progress.
- Invariant 2:
  - Continuation cutoff is fixed for a continuation sequence.
- Invariant 3:
  - Cursor progression and watermark updates are performed under repo lock to avoid conflicting transitions.
- Subtlety A:
  - Dynamic enqueue caps may enqueue fewer PRs than discovered. Discovery completion and enqueue completion are intentionally decoupled; continuation still needed until scan coverage is complete.
- Subtlety B:
  - Duplicate `sync_pr` enqueue is acceptable. `sync_pr` remains idempotent via header skip + upsert behavior.
- Subtlety C:
  - If continuation state is stale/corrupt (invalid cursor), fail safe by clearing continuation and restarting fresh with overlap; emit warning metrics.

## Chunked Implementation Plan

### Chunk 1: Model + migration + read/write scaffolding
- Add `RepoDiscoveryState` model and migration.
- Register in admin for visibility (read-only fields where appropriate).
- Add minimal model tests and import wiring.

### Chunk 2: Client discovery contract
- Implement structured discovery helper returning progress metadata (`next_cursor`, `reached_cutoff`, `hit_limit`).
- Add unit tests for cutoff, limit, and cursor behavior.

### Chunk 3: Task mode/state transitions
- Update `sync_repo_since_task` to use fresh/continuation modes.
- Persist continuation state and watermark transitions with strict invariants.
- Keep current lock and rate-limit defer paths.

### Chunk 4: Continuation scheduling for non-rate cap exhaustion
- Ensure continuation is scheduled when local discovery cap is hit before cutoff completion.
- Add debounce/guard parity with existing rate defer scheduling.

### Chunk 5: Observability
- Extend convergence/metrics snapshots with:
  - discovery lag,
  - continuation active flag,
  - last attempted/success timestamps.
- Add admin display for quick diagnosis.

### Chunk 6: Tests and failure-path hardening
- Outage recovery test:
  - no sync for > lookback; recovery eventually enqueues missed updates.
- High churn test:
  - > discovery limit within window; continuation covers all pages.
- Partial-failure test:
  - watermark does not advance on incomplete runs.
- Boundary test:
  - overlap prevents equality/race misses around cutoff timestamps.

## Validation Plan
- Unit tests:
  - discovery helper pagination semantics,
  - task state machine transitions,
  - watermark invariants.
- Integration-style task tests:
  - sequential continuation runs over mocked discovery pages,
  - defer/retry behavior with persisted continuation.
- Manual verification:
  - inspect `RepoDiscoveryState` transitions for one active repo in staging/local,
  - confirm lag metric decreases to steady-state.

## Operational Rollout
- Phase 1:
  - ship model + task logic with metrics.
- Phase 2:
  - observe lag/backlog behavior for several days.
- Phase 3:
  - tune overlap and per-run limits if needed.

## Deferred / Follow-up Work (Not Immediate)
- Queue isolation (`SYNCER_GITHUB_QUEUE` plus dedicated worker) is deferred.
- Analyzer pressure/sweep tuning is not part of this implementation:
  - `ANALYZER_QUEUE_WINDOWS_SWEEP_MAX_PRS_PER_REPO` has already been lowered operationally.
- Additional scheduling fairness work can be revisited after discovery correctness is restored.

## References
- `qb_site/syncer/tasks/sync_tasks.py`
- `qb_site/syncer/services/github_client.py`
- `qb_site/syncer/models/repo_backfill_cursor.py`
- `qb_site/syncer/tasks/backfill_tasks.py`
- `qb_site/syncer/tasks/collect_convergence.py`
- `qb_site/qb_site/settings/base.py`
