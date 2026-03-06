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
  - TTL: `SYNCER_SYNC_PR_DEDUPE_TTL_SECONDS` (proposed default `1800`).
- `syncer.sync_ci_for_shas`:
  - key parts: `repo_id:number:max_pages_per_sha:sorted(shas)`
  - TTL: `SYNCER_SYNC_CI_DEDUPE_TTL_SECONDS` (proposed default `900`).
  - For webhook-driven check-event bursts, start with a much shorter effective TTL (candidate `60-120s`) and tune upward only if needed.

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
  - `SYNCER_SYNC_PR_DEDUPE_TTL_SECONDS` (default `1800`),
  - `SYNCER_SYNC_CI_DEDUPE_TTL_SECONDS` (default `900`; webhook-first rollout candidate override `60-120`).
- Runtime dedupe (phase 2):
  - `SYNCER_SYNC_PR_RUNTIME_DEDUPE_TTL_SECONDS` (default TBD).

## Chunked Implementation Plan
1. Redis helper and enqueue dedupe utility.
2. Apply enqueue dedupe to `sync_ci_for_shas` producers (highest priority from observed backlog).
3. Add enqueue dedupe counters/metrics for CI fanout suppression and validate queue slope improvement.
4. Apply enqueue dedupe to `sync_pr` producers.
5. Add runtime dedupe to `sync_pr_task`.
6. Add tests for dedupe behavior and fail-open semantics.
7. Document operational tuning (TTLs and source-specific behavior).

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

## References
- `qb_site/syncer/tasks/sync_tasks.py`
- `qb_site/syncer/tasks/backfill_tasks.py`
- `qb_site/syncer/tasks/commit_history_tasks.py`
- `qb_site/syncer/services/rate_budget.py`
- `qb_site/qb_site/settings/base.py`
- `docs/design-decisions/029-updatedat-discovery-watermark-and-catchup.md`
