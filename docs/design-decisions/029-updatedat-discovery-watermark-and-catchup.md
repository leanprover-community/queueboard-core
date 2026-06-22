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
  - `continuation_success_cutoff`: the `fresh_base_cutoff` captured when the fresh
    scan spawned this continuation; used as the watermark target when the continuation
    completes (see Stale-Watermark Trap below).
  - `last_attempted_at`: last discovery attempt timestamp.
  - `last_successful_at`: last successful full-scan timestamp.
- Helper transitions:
  - `mark_attempted()`
  - `set_continuation(cutoff_at, cursor, success_cutoff=None)`
  - `mark_success(cutoff_at)` (also clears all continuation fields).

### Backfill Seed Semantics
- Timeline/commit backfill completion flags are seeded from bundle pageInfo with monotone semantics.
- For unfiltered bundles:
  - if `hasPreviousPage=False`, mark `*_backfill_done=True` even when `startCursor` is absent.
- For filtered timeline bundles (`timelineSince` present):
  - do not mark timeline backfill complete from filtered pageInfo alone.
- This avoids "stuck pending" rows where `*_backfill_done=False` and cursor is null forever.

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
  3. Compute cutoff(s):
     - fresh mode computes:
       - `base_cutoff` (target boundary): current sliding-window cutoff,
       - `scan_start_cutoff` (query start): `min(base_cutoff, last_successful_cutoff_at - overlap)`.
     - continuation mode uses fixed persisted continuation cutoff; `success_cutoff`
       is taken from `continuation_success_cutoff` (set by the originating fresh run).
  4. Run structured discovery.
  5. Enqueue `sync_pr` tasks under existing dynamic batch/rate budget rules,
     tracking `undrained` — discovered numbers not enqueued or already in flight
     this tick (see Undrained-Tail Coverage below). Dedupe-skips (already in
     flight) do not consume budget, so the budget reaches *new* numbers further
     down the list rather than burning on the positional head every rescan.
  6. Persist state transition (uses both scan completion *and* `undrained`):
     - complete scan (`reached_cutoff` or no `next_cursor`) **and `undrained == 0`**:
       `mark_success`,
     - incomplete scan: `set_continuation`; fresh/fresh_recovery runs pass
       `fresh_base_cutoff` as `success_cutoff` so it is stored for later use,
     - complete scan but `undrained > 0`: hold the watermark and clear any
       continuation cursor, so the next tick re-scans the same (held) window
       rather than resuming forward.
  7. Schedule continuation when work remains (`not scan_complete or undrained > 0`).

### Continuation Scheduling
- Low-budget path (existing behavior retained):
  - if remaining GitHub budget is low, defer to `resetAt` with debounce.
- Cap/page exhaustion path (new):
  - if scan incomplete even with healthy budget, schedule near-term continuation.
- Undrained-tail path (new): scan reached the cutoff but the batch cap left
  numbers `undrained`; schedule a near-term drain (`undrained_tail`) that
  re-scans the held window. The drain debounce key varies with drain progress
  (`drain:{cutoff}:{enqueued}:{prs_skipped_dedupe}`) so successive ticks are not
  suppressed; the delay stays within the per-PR enqueue dedupe TTL so already-
  enqueued numbers are skipped and the budget reaches the tail.
- Both paths use `debounce_repo_schedule(...)` to suppress duplicate scheduling.
- Continuation reasons are surfaced in task result (`low_budget` /
  `cap_exhausted` / `undrained_tail`).

### Failure Handling
- If continuation discovery fails (for example stale/invalid cursor):
  - clear continuation cursor/cutoff/start,
  - retry immediately in fresh recovery mode using overlap logic.
- This fail-safe prefers duplicate work over missed updates.

## Invariants
- Watermark invariant:
  - `last_successful_cutoff_at` represents fully scanned coverage only.
  - It must not advance on partial discovery progress.
  - It advances only when the scan reached the cutoff **and** every discovered
    number was enqueued or already in flight (`undrained == 0`); a discovered
    tail left undrained by the batch cap holds the watermark (see
    Undrained-Tail Coverage). Note the caveat there: the hold protects
    single-page and low-budget ticks, not the multi-page continuation cursor.
  - In fresh mode, successful completion advances watermark to `base_cutoff` (the target boundary), not to older overlap-expanded scan start.
- Continuation invariant:
  - continuation cutoff is fixed for a continuation sequence.
- Serialization invariant:
  - state transitions occur under per-repo advisory lock.
- Idempotency invariant:
  - duplicate PR enqueue is acceptable; downstream sync remains idempotent.

## Observability
- `SyncerConvergenceSnapshot` includes discovery diagnostics:
  - `discovery_lag_seconds`
  - `discovery_catchup_lag_seconds`: seconds between `last_successful_cutoff_at` and
    `continuation_success_cutoff`; non-null only while a catch-up continuation is
    active.  Should trend toward zero as the catch-up makes progress.
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
  - `syncer` migrations `0028` (discovery state), `0029` (convergence observability),
    and `0038` (stale-watermark fix: `continuation_success_cutoff` +
    `discovery_catchup_lag_seconds`).

## Consequences
- Pros:
  - robust recovery from long outages and high update churn,
  - explicit progress semantics for discovery coverage,
  - improved operational visibility for lag and continuation health.
- Trade-offs:
  - more state complexity in repo sync task,
  - intentional duplicate work in overlap/recovery paths to preserve correctness.

## Implementation Nuance
- Overlap is a scan-safety mechanism only.
  - It may expand where a fresh scan starts.
  - It must not anchor successful watermark advancement to an ever-older cutoff.
- If watermark advancement incorrectly uses overlap-expanded scan start, discovery lag can drift upward despite successful runs; the implemented design avoids this by advancing to fresh `base_cutoff`.

## Stale-Watermark Trap (and Fix)
- When `last_successful_cutoff_at` falls far behind `now() - lookback_minutes`, the
  overlap logic (`min(base_cutoff, watermark - overlap)`) forces every fresh scan to
  cover the entire stale gap.  With a finite discovery limit that gap exceeds one
  batch, spawning a continuation.
- **Bug (pre-fix):** continuation mode set `success_cutoff = continuation_cutoff_at`
  (the old scan boundary).  So even after successfully paginating through the entire
  gap, `mark_success` set the watermark back to the stale date — immediately spawning
  another giant continuation.  The watermark could never escape.
- **Fix:** when a fresh (or fresh_recovery) run starts a continuation it stores
  `fresh_base_cutoff` in `continuation_success_cutoff`.  Continuation-mode batches
  inherit this value unchanged.  When the continuation finally completes, `mark_success`
  uses `continuation_success_cutoff` as the new watermark, advancing it to near-now.
- The `discovery_catchup_lag_seconds` snapshot field makes this backlog visible in
  the admin: it reports `continuation_success_cutoff - last_successful_cutoff_at` while
  a catch-up is running, and becomes null once it completes.
- Schema change: migration `0038` adds `continuation_success_cutoff` to
  `RepoDiscoveryState` and `discovery_catchup_lag_seconds` to
  `SyncerConvergenceSnapshot`.

## Undrained-Tail Coverage (and Continuation Caveat)
- **Motivation.** A single discovery tick can discover more numbers than the
  dynamic batch/rate budget can enqueue. The pre-fix code advanced the watermark
  on `scan_complete` alone, so any numbers beyond the batch cap — and, on a
  low-budget tick, *every* discovered number (the enqueue block was skipped
  entirely) — were stepped over. Open PRs are eventually rediscovered when their
  `updatedAt` moves, but a **closed** PR has a frozen `updatedAt`: once the
  watermark passes it, a fresh scan never revisits it. This is how a closed PR
  gets stranded on the queue with no staleness signal to recover it.
- **Fix.** The enqueue loop tracks `undrained` (discovered numbers neither
  enqueued nor already in flight this tick). `mark_success` requires
  `scan_complete and undrained == 0`. When the scan reached the cutoff but a tail
  is undrained, the watermark is held, any continuation cursor is cleared, and an
  `undrained_tail` drain continuation re-scans the same window. Because
  dedupe-skips do not consume budget and the drain fires within the per-PR enqueue
  dedupe TTL, each successive tick skips the already-enqueued head and spends the
  budget on the tail, draining to `undrained == 0` over a bounded number of ticks
  before the watermark finally advances.
- **Caveat — multi-page continuation under a rate cap.** The hold-and-rescan
  above protects single-page fresh scans (`scan_complete`) and low-budget ticks.
  It does **not** cover continuation mode when discovery hit its count limit
  (`next_cursor` set ⇒ `scan_complete == False`) *and* the rate budget capped the
  batch: the `set_continuation` branch advances the continuation *cursor* to
  `next_cursor`, stepping past the undrained tail of that page. Holding the cursor
  instead is not viable — discovery stops at `SYNCER_DISCOVERY_LIMIT`, so a held
  cursor would re-scan only the first page forever and never paginate deeper.
  Those skipped numbers are recovered by the next fresh-scan overlap (open PRs) or
  by the consistency reconciler — `inconsistent_open_prs_queryset` re-enqueues
  closed-but-open / draft-drift rows for a self-healing sync (see
  `qb_site/syncer/services/consistency.py` and the `inconsistent_open_prs`
  convergence metric). The watermark invariant ("never advance past an
  un-enqueued PR") is therefore tight for the common path; the continuation +
  rate-cap path relies on these secondary layers rather than the held-watermark
  rescan. Closing it fully (e.g. tracking per-page undrained offsets within a
  continuation) is deferred — it is rarely reached under the default OPEN-only
  discovery, and the reconciler bounds the blast radius.
- Schema change: migration `0051` adds `inconsistent_open_prs` to
  `SyncerConvergenceSnapshot`.

## Out of Scope
- Queue isolation / dedicated GitHub queue strategy changes.
- Analyzer scheduling redesign.
- CreatedAt history backfill policy changes outside updatedAt discovery flow.

## References
- `qb_site/syncer/models/repo_discovery_state.py`
- `qb_site/syncer/services/github_client.py`
- `qb_site/syncer/services/consistency.py`
- `qb_site/syncer/tasks/sync_tasks.py`
- `qb_site/syncer/tasks/backfill_tasks.py`
- `qb_site/syncer/tasks/collect_convergence.py`
- `qb_site/syncer/models/convergence_snapshot.py`
- `qb_site/syncer/admin.py`
- `qb_site/qb_site/settings/base.py`
