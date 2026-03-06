# Sync Task Dedupe Strategy

## Context
- Syncer task producers could enqueue large duplicate volumes for:
  - `syncer.sync_ci_for_shas` (especially check-event bursts),
  - `syncer.sync_pr` (repo discovery, backfills, webhook, admin fanout).
- Existing task logic was idempotent for correctness, but queue/worker capacity was wasted under duplicate pressure.
- Queue backlog made enqueue-time dedupe alone insufficient: duplicates could still execute later if enqueue TTL expired while tasks waited.
- Redis is shared infrastructure and can be unavailable or degraded, so dedupe must not drop required work when Redis fails.

## Decision
- Implement two-layer dedupe for Syncer:

### 1) Enqueue-time dedupe (producer-side)
- Use Redis `SET NX EX` per task identity.
- If key claim fails, suppress enqueue.
- If Redis client init or Redis operation fails, fail-open (allow enqueue).
- Implemented helper: `qb_site/syncer/services/task_dedupe.py`.
- Key identities:
  - `sync_pr`: `repo_id:number`
  - `sync_ci_for_shas`: `repo_id:number:max_pages_per_sha:sorted(shas)` (SHA set normalized and hashed)

### 2) Runtime dedupe (execution-side, `sync_pr` only)
- Add short-lived runtime guard at `sync_pr_task` entry.
- If runtime key claim fails and `force=False`, skip execution with explicit summary:
  - `status=runtime_deduped`,
  - `reason=recently_processed`.
- `force=True` bypasses runtime dedupe.

### 3) Separate key namespaces
- Enqueue and runtime dedupe use different Redis key prefixes:
  - enqueue: `syncer:dedupe:enqueue:*`
  - runtime: `syncer:dedupe:runtime:*`
- This prevents self-suppression caused by reusing the same key for enqueue and execution phases.

### 4) Producer coverage
- `sync_ci_for_shas` dedupe applied to:
  - webhook check routing,
  - pending-CI refresh,
  - commit-history CI fanout,
  - admin CI enqueue.
- `sync_pr` dedupe applied to:
  - repo discovery fanout,
  - history/incomplete/engagement backfills,
  - webhook pull_request routing,
  - admin PR enqueue (single and bulk).

### 5) Observability
- Delivery-level webhook reasons now include dedupe outcomes:
  - `deduped_sync_ci`
  - `deduped_sync_pr`
- Producer/task summaries include dedupe counters (for example `deduped_sync_ci`, `deduped_sync_prs`, `prs_skipped_dedupe`, backfill `deduped`).
- `SyncerMetricsSnapshot` includes dedupe-focused webhook metrics:
  - `webhook_reason_deduped_sync_ci`
  - `webhook_deduped_sync_ci_total`
  - `webhook_reason_deduped_sync_pr`
  - `webhook_deduped_sync_pr_total`

### 6) Defaults
- `SYNCER_SYNC_CI_DEDUPE_TTL_SECONDS=300`
- `SYNCER_SYNC_PR_DEDUPE_TTL_SECONDS=300`
- `SYNCER_SYNC_PR_RUNTIME_DEDUPE_TTL_SECONDS=300`

## Consequences
- Benefits:
  - materially lower duplicate queue growth for CI and PR sync fanout,
  - lower redundant worker execution under backlog,
  - explicit operational visibility for dedupe behavior.
- Trade-offs:
  - dedupe is best-effort optimization, not exactly-once guarantee,
  - enqueue-time TTL dedupe can still admit duplicates after TTL expiry while old tasks are queued,
  - strict in-flight exclusivity is not guaranteed by this design (no durable lock lifecycle/state machine).
- Invariants preserved:
  - correctness remains anchored by idempotent sync logic,
  - Redis failures do not silently drop required work (fail-open).

## Operational Notes
- Current status:
  - enqueue dedupe (`sync_ci_for_shas`, `sync_pr`) and runtime dedupe (`sync_pr`) are implemented and deployed.
- Monitoring focus:
  - queue depth/drain behavior,
  - webhook reason mix (`enqueued_*` vs `deduped_*`),
  - dedupe totals (`webhook_deduped_sync_ci_total`, `webhook_deduped_sync_pr_total`),
  - freshness indicators (time-to-CI update, time-to-PR refresh).
- Tuning guidance:
  - lower TTLs if freshness regresses and dedupe totals are high,
  - raise TTLs if duplicate pressure remains high and freshness is acceptable,
  - change one knob at a time and observe at least one full daily cycle.
- Emergency behavior:
  - keep fail-open semantics intact during Redis incidents,
  - reduce periodic producer pressure separately if queue drain is required.

### Deferred follow-up
- Evaluate runtime dedupe for `sync_ci_for_shas` after longer production observation.
- Evaluate source-sensitive dedupe keys only if operations require finer control.
- Continue architectural work to reduce PR fanout in check routing:
  - `docs/design-decisions/035-sha-first-ci-sync-task-and-webhook-fanout.md`.

## Alternatives
- Enqueue-time dedupe only:
  - rejected as sufficient alone because backlog + TTL expiry can still allow redundant execution.
- Strict in-flight lock lifecycle (owner tokens/heartbeat/unlock) as immediate default:
  - deferred due to higher complexity and failure-mode surface; current design preferred for fast risk-reducing rollout.
- No Redis fail-open:
  - rejected because infra failures could otherwise silently suppress required sync work.

## References
- `qb_site/syncer/services/task_dedupe.py`
- `qb_site/syncer/tasks/sync_tasks.py`
- `qb_site/syncer/tasks/backfill_tasks.py`
- `qb_site/syncer/tasks/commit_history_tasks.py`
- `qb_site/syncer/views.py`
- `qb_site/syncer/models/metrics.py`
- `qb_site/syncer/tasks/metrics_tasks.py`
- `qb_site/syncer/admin.py`
- `qb_site/qb_site/settings/base.py`
- `docs/design-decisions/034-github-webhook-ingestion-for-syncer.md`
- `docs/design-decisions/035-sha-first-ci-sync-task-and-webhook-fanout.md`
