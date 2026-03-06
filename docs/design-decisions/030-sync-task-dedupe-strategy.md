# Sync Task Dedupe Strategy (Living Plan)

## Context
- The `default` Celery queue can accumulate large duplicate volumes for:
  - `syncer.sync_pr` (same repo/PR enqueued repeatedly),
  - `syncer.sync_ci_for_shas` (same repo/PR/sha sets enqueued repeatedly).
- Queue backlog can cause delayed processing and bursty execution even when individual tasks are short.
- Existing idempotency in task logic prevents data corruption, but does not prevent wasted queue/worker capacity.

## Recent Observations (2026-03-06, production webhook trial)
- Webhook delivery sample (~few hours):
  - `enqueued_sync_ci`: `7145` deliveries (dominant queue source)
  - `enqueued_sync_pr`: `281` deliveries
  - `no_pr_resolution`: `2170` deliveries (non-enqueue noise)
- High duplicate pressure on CI fanout:
  - many repeated `(repo, head_sha)` check events per minute, including spikes over `100` deliveries for a single SHA in one minute.
- Practical conclusion:
  - enqueue dedupe for `syncer.sync_ci_for_shas` is the highest-priority mitigation.
  - `sync_pr` dedupe still useful, but secondary for queue stabilization.

## Implementation Snapshot (2026-03-06 code audit)
- Already present (related primitives):
  - Repo continuation dedupe exists for discovery continuation scheduling only:
    - `syncer.services.rate_budget.debounce_repo_schedule`
    - used in `syncer.sync_repo_since` continuation enqueue path.
  - CI retry/backoff gating exists via `CIShaFetchState`:
    - `syncer.services.ci_backoff.should_enqueue_ci_sha(_with_state)`
    - used by commit-history and pending-CI refresh producers.
- Not yet present (scope of this plan):
  - no generic enqueue-time dedupe helper for `sync_pr` / `sync_ci_for_shas` task signatures.
  - no broad producer-side dedupe wrapping before `.delay()`/`enqueue_with_parent(...)`.
  - no runtime dedupe guard in `sync_pr_task` (or `sync_ci_for_shas_task`).
  - no dedicated dedupe settings in `qb_site/settings/base.py` for this strategy.

## Producer Inventory To Cover
- `sync_pr` enqueue producers:
  - `syncer.tasks.sync_tasks.sync_repo_since_task`
  - `syncer.tasks.backfill_tasks` (history/incomplete/engagement backfills)
  - webhook endpoint path in `syncer.views`
  - manual admin enqueue paths in `syncer.admin`
- `sync_ci_for_shas` enqueue producers:
  - webhook endpoint path in `syncer.views` (highest observed duplicate source)
  - `syncer.tasks.sync_tasks.refresh_pending_ci_for_repo_task`
  - `syncer.tasks.commit_history_tasks.harvest_commit_history_task`
  - manual admin enqueue paths in `syncer.admin`

## Problem Statement
- We need to reduce redundant sync workload in two places:
  - enqueue-time (prevent duplicate tasks entering queue),
  - run-time (skip tasks that are stale/redundant when they finally execute).
- The solution must preserve correctness under outages and retries.

## Goals / Non-Goals
- Goals:
  - lower queue growth from duplicate sync work,
  - reduce redundant worker execution under backlog,
  - keep fail-open behavior when Redis is unavailable.
- Non-goals:
  - strict exactly-once processing guarantees,
  - immediate queue compaction of already-enqueued historical backlog,
  - queue isolation redesign (separate queue/worker topology).

## Proposed Design

### A) Enqueue-Time Dedupe
- Use Redis `SET NX EX` per task identity.
- If lock acquisition fails, suppress enqueue.
- If Redis unavailable/errors, allow enqueue (fail-open).

Candidate identities:
- `syncer.sync_pr`:
  - key parts: `repo_id:number`
  - TTL: `SYNCER_SYNC_PR_DEDUPE_TTL_SECONDS` (current default `300`).
- `syncer.sync_ci_for_shas`:
  - key parts: `repo_id:number:max_pages_per_sha:sorted(shas)`
  - TTL: `SYNCER_SYNC_CI_DEDUPE_TTL_SECONDS` (current default `300`).

Primary producer paths to cover:
- repo discovery fanout (`sync_repo_since_task`),
- incomplete/history/engagement backfills,
- pending-CI refresh fanout,
- commit-history-triggered CI sync.

### B) Run-Time Dedupe (Backlog Relief)
- Add a short-lived "recently processed" guard at task start.
- Initial scope: `syncer.sync_pr` only (keep CI dedupe enqueue-time first; runtime CI dedupe remains optional follow-up).
- Behavior:
  - if recent marker exists and `force=False`: skip with explicit reason,
  - otherwise set marker and execute.
- Proposed setting:
  - `SYNCER_SYNC_PR_RUNTIME_DEDUPE_TTL_SECONDS` (default candidate `300`-`900`).

## Invariants and Safety
- Dedupe is optimization-only; correctness remains anchored by idempotent sync logic.
- Dedupe must not convert transient infrastructure failures into dropped work:
  - Redis failures -> fail-open.
- Task summaries should include dedupe counters/reasons for observability.

## Observability Plan
- Emit/return counters:
  - enqueue deduped count per producer task,
  - runtime skipped count/reason (`recently_processed`).
- Add lightweight metrics logs keyed by task name and dedupe reason.
- Keep convergence/admin checks focused on queue pressure and sync freshness.

## Settings Plan
- Enqueue dedupe:
  - `SYNCER_SYNC_PR_DEDUPE_TTL_SECONDS` (default `300`),
  - `SYNCER_SYNC_CI_DEDUPE_TTL_SECONDS` (default `300`).
- Runtime dedupe (phase 2):
  - `SYNCER_SYNC_PR_RUNTIME_DEDUPE_TTL_SECONDS` (default `300`).

## Chunked Implementation Plan
1. Introduce Redis enqueue-dedupe helper for sync tasks (fail-open on Redis errors).
2. Add settings:
   - `SYNCER_SYNC_CI_DEDUPE_TTL_SECONDS`
   - `SYNCER_SYNC_PR_DEDUPE_TTL_SECONDS`
3. Apply enqueue dedupe to `sync_ci_for_shas` producers first:
   - webhook + refresh-pending + commit-history + admin paths.
4. Add dedupe counters/summary fields for CI producer tasks and validate suppression ratio.
5. Apply enqueue dedupe to `sync_pr` producers:
   - repo discovery + backfill tasks + webhook + admin paths.
6. Add runtime dedupe in `sync_pr_task`:
   - setting `SYNCER_SYNC_PR_RUNTIME_DEDUPE_TTL_SECONDS`
   - `force=True` bypass.
7. Add tests for:
   - allow-first/suppress-duplicate behavior,
   - fail-open on Redis unavailable/error,
   - runtime skip + `force=True` bypass.
8. Capture rollout tuning notes from production observations.

## Validation Plan
- Unit tests:
  - `SET NX EX` behavior (allow first, suppress duplicate),
  - fail-open on Redis errors/unavailable.
- Task tests:
  - producer suppresses duplicate enqueues,
  - runtime dedupe skips repeated execution within TTL,
  - `force=True` bypasses runtime dedupe.
- Ops validation:
  - compare queue depth slope before/after,
  - monitor reduction in duplicate task keys in sampled queue messages.

## Operational Rollout
- Phase 1: enable enqueue dedupe for `sync_ci_for_shas` first with short TTL (`60-120s`) and observe queue slope.
- Phase 2: tune CI TTL upward/downward based on suppression ratio and freshness.
- Phase 3: enable enqueue dedupe for `sync_pr`.
- Phase 4: add runtime dedupe for `sync_pr` with short TTL.
- Keep temporary knobs available to disable periodic producers during emergency drain.

## Open Questions
- Should runtime dedupe include `sync_ci_for_shas` or remain `sync_pr`-only initially?
- What TTL balances freshness vs duplicate suppression for very active PRs?
- Should dedupe keys include operation source labels for finer-grained control?

## Progress Notes
- 2026-03-06:
  - Added production webhook trial observations and prioritized `sync_ci_for_shas` enqueue dedupe.
  - Audited code paths and confirmed this plan is still largely unimplemented (except continuation debounce + CI backoff primitives).
  - Expanded explicit producer inventory so implementation can proceed incrementally without missing enqueue sources.
  - Chunk 1 completed:
    - added enqueue dedupe helper in `qb_site/syncer/services/task_dedupe.py`
      - `sync_pr_enqueue_key(...)`
      - `sync_ci_enqueue_key(...)` (sorted/unique SHA canonicalization + digest)
      - `claim_enqueue_slot(...)` using Redis `SET NX EX` with fail-open semantics
    - added focused tests in `qb_site/syncer/tests/services/test_task_dedupe.py`
      - first-writer wins / duplicate suppression behavior
      - fail-open when Redis client unavailable or errors
      - deterministic key normalization for CI SHA sets
    - validation:
      - `uv run ruff check qb_site/syncer/services/task_dedupe.py qb_site/syncer/tests/services/test_task_dedupe.py` passed
      - `uv run python qb_site/manage.py test syncer.tests.services.test_task_dedupe` blocked locally (Postgres not running in this environment)
  - Chunk 2 completed:
    - added settings in `qb_site/qb_site/settings/base.py`:
      - `SYNCER_SYNC_CI_DEDUPE_TTL_SECONDS` (default `300`)
      - `SYNCER_SYNC_PR_DEDUPE_TTL_SECONDS` (default `300`)
    - added matching environment knobs to `.env.example` for discoverability/ops tuning.
    - validation:
      - `uv run ruff check qb_site/qb_site/settings/base.py` passed
      - `uv run ruff format .env.example` is not applicable (non-Python file)
  - Updated defaults after review:
    - lowered enqueue dedupe defaults to `300s` for both CI and PR to balance duplicate suppression vs freshness under webhook-driven bursts.
  - Chunk 3 completed (`sync_ci_for_shas` producer dedupe):
    - applied enqueue dedupe to CI producer paths using `sync_ci_enqueue_key(...)` + `claim_enqueue_slot(...)`:
      - webhook check-event fanout (`syncer/views.py`)
      - pending-CI refresh fanout (`syncer/tasks/sync_tasks.py`)
      - commit-history-triggered CI sync (`syncer/tasks/commit_history_tasks.py`)
      - manual admin enqueue action (`syncer/admin.py`)
    - added CI producer visibility fields where summaries already exist:
      - webhook summary: `deduped_sync_ci`
      - pending-CI refresh summary: `prs_skipped_dedupe`, `shas_skipped_dedupe`
    - added targeted tests for dedupe suppression paths:
      - `syncer/tests/test_github_webhook_endpoint.py`
      - `syncer/tests/tasks/test_refresh_pending_ci_task.py`
      - `syncer/tests/tasks/test_commit_history_tasks.py`
      - `syncer/tests/admin/test_enqueue_ci_sha.py`
    - validation:
      - `uv run ruff check` passed on all touched Python files
      - targeted Django tests blocked locally (Postgres not running in this environment)
  - Post-chunk observability follow-up:
    - webhook check-event routing now emits explicit `reason=deduped_sync_ci` when all candidate CI enqueues are dedupe-suppressed.
    - `SyncerMetricsSnapshot` now tracks dedupe-focused webhook metrics:
      - `webhook_reason_deduped_sync_ci` (delivery count)
      - `webhook_deduped_sync_ci_total` (sum of `summary_json.deduped_sync_ci` across deliveries in window)
    - migration added: `0034_syncermetricssnapshot_webhook_deduped_sync_ci_total_and_more.py`.
  - Chunk 5 completed (`sync_pr` producer dedupe):
    - applied enqueue dedupe to PR producer paths using `sync_pr_enqueue_key(...)` + `claim_enqueue_slot(...)`:
      - repo discovery fanout in `syncer.sync_repo_since`
      - history/incomplete/engagement backfills in `syncer/tasks/backfill_tasks.py`
      - webhook pull_request routing in `syncer/views.py`
      - admin manual enqueue actions in `syncer/admin.py` (single-item and bulk actions)
    - added summary visibility where available:
      - repo discovery summary: `prs_skipped_dedupe`
      - webhook pull_request summary: `deduped_sync_prs`, plus `reason=deduped_sync_pr` when fully suppressed
      - backfill summaries include `deduped` counts
    - added targeted tests:
      - webhook pull_request dedupe suppression in `syncer/tests/test_github_webhook_endpoint.py`
      - repo discovery dedupe suppression in `syncer/tests/tasks/test_sync_repo_tasks.py`
      - history backfill dedupe suppression in `syncer/tests/backfill/test_repo_history_backfill_task.py`
    - validation:
      - `uv run ruff check` passed on all touched Python files
      - targeted Django tests blocked locally (Postgres not running in this environment)
  - Chunk 6 completed (`sync_pr` runtime dedupe):
    - added dedicated runtime key namespace in `syncer/services/task_dedupe.py`:
      - `sync_pr_runtime_key(...)`
      - `claim_runtime_slot(...)`
    - added runtime dedupe guard at start of `syncer.sync_pr_task`:
      - when `force=False` and runtime key already claimed within TTL:
        - returns `status=runtime_deduped`, `reason=recently_processed`
      - `force=True` explicitly bypasses runtime dedupe
    - added setting/env knob:
      - `SYNCER_SYNC_PR_RUNTIME_DEDUPE_TTL_SECONDS` (default `300`)
      - surfaced in `qb_site/qb_site/settings/base.py` and `.env.example`
    - added targeted tests:
      - runtime skip path in `syncer/tests/tasks/test_sync_pr_task_skip.py`
      - force bypass path in `syncer/tests/tasks/test_sync_pr_task_skip.py`
      - runtime key helper coverage in `syncer/tests/services/test_task_dedupe.py`
    - validation:
      - `uv run ruff check` passed on all touched Python files
      - targeted Django tests blocked locally (Postgres not running in this environment)
  - Chunk 7 completed (test/fail-open closeout):
    - hardened dedupe helper fail-open behavior:
      - `claim_enqueue_slot(...)` now fail-opens if Redis client acquisition itself raises.
      - `claim_runtime_slot(...)` now fail-opens on unexpected delegation errors.
    - extended unit coverage in `syncer/tests/services/test_task_dedupe.py`:
      - Redis client factory exception path
      - runtime claim unexpected-exception fail-open path
    - added/expanded producer summary coverage for dedupe counters:
      - incomplete backfill and engagement backfill tests assert `deduped` fields
      - added suppression tests for both backfill producers when dedupe blocks enqueue
    - validation:
      - `uv run ruff check` passed on touched files
      - targeted Django tests blocked locally (Postgres not running in this environment)
  - Metrics follow-up:
    - extended `SyncerMetricsSnapshot` webhook dedupe tracking to PR sync dedupe as well:
      - `webhook_reason_deduped_sync_pr` (delivery count)
      - `webhook_deduped_sync_pr_total` (sum of `summary_json.deduped_sync_prs`)
    - updated collector/admin/tests and added migration `0035_syncermetricssnapshot_webhook_deduped_sync_pr_total_and_more.py`.

## References
- `qb_site/syncer/tasks/sync_tasks.py`
- `qb_site/syncer/tasks/backfill_tasks.py`
- `qb_site/syncer/tasks/commit_history_tasks.py`
- `qb_site/syncer/services/rate_budget.py`
- `qb_site/qb_site/settings/base.py`
- `docs/design-decisions/029-updatedat-discovery-watermark-and-catchup.md`
- `docs/design-decisions/035-sha-first-ci-sync-task-and-webhook-fanout.md` (follow-up architecture split for SHA-first CI tasking)
