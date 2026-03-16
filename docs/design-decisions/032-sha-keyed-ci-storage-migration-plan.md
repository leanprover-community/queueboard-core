# SHA-Keyed CI Storage Migration

## Context
- Decision `019` split this area into:
  - Part 1: CI-by-SHA backoff ledger (`CIShaFetchState`).
  - Part 2: migration from PR-keyed CI storage to SHA-keyed CI storage.
- CI facts are commit-scoped by nature, but historical storage/read paths used PR-keyed tables (`CheckRun`, `StatusContext`).
- PR-keyed CI storage introduced ambiguity across force-pushes/revisions and duplicated CI state when SHAs appeared across PR contexts.
- The migration was executed in staged rollout phases with dual-write/read safety gates, then converged to SHA-only runtime behavior.

## Decision
- Use SHA-keyed CI tables as the runtime source of truth:
  - `syncer.CommitCheckRun`
  - `syncer.CommitStatusContext`
- Key CI evaluation and planning on `(repository, head_sha)` via PR revision windows.
- Keep `CIShaFetchState` as the enqueue/backoff control plane for CI-by-SHA fetch policy.
- Remove runtime dependence on PR-keyed CI tables (`CheckRun`, `StatusContext`) for:
  - ingest/write paths,
  - analyzer queue/snapshot/revision CI reads,
  - pending-CI refresh selection,
  - convergence counting.

### Implemented Architecture
- Commit-scoped CI persistence:
  - New models and indexes/constraints added for SHA-keyed CI facts.
  - Ingest upserts commit-scoped rows directly.
- Analyzer CI consumption:
  - Queue windows and queueboard snapshot CI status derive from commit-scoped rows.
  - Revision rebuild and missing-CI planning derive CI presence from commit-scoped rows.
- Syncer operational tasks:
  - Pending-CI refresh and convergence collectors use commit-scoped CI presence.
- Backfill and rollout:
  - Idempotent backfill command populated SHA-keyed tables from legacy PR-keyed rows.
  - Dual-write/dual-read soak completed; fallback paths were retired.

## Consequences
- Correctness and consistency improvements:
  - CI is evaluated against commit identity directly, matching force-push/revision semantics.
  - Shared SHA behavior is no longer coupled to PR-scoped duplication.
- Operational simplification:
  - Runtime no longer requires transitional SHA/PR feature flags.
  - No active production decision path depends on PR-keyed CI rows.
- Trade-off:
  - Commit-scoped tables may accumulate historical snapshot rows over time.
  - Current reads are latest-wins, so correctness is preserved without inline pruning.

## Operational Notes
- Current status:
  - Migration is complete for runtime behavior.
  - Legacy PR-keyed CI tables have been removed.
- Planner correctness nuance:
  - Missing-CI planner enqueues only `actionable_shas` after backoff gating.
  - Backoff-blocked planned SHAs remain retryable in later sweeps.
- Deferred follow-up:
  - Commit-scoped CI compaction/pruning is intentionally deferred and should be bundled with broader data-hygiene cleanup work.

## Alternatives Considered
- Keep PR-keyed CI storage as primary runtime source:
  - Rejected due to ambiguity for revision/head-SHA semantics and duplicated CI representation.
- Continue dual-read/dual-write indefinitely:
  - Rejected to avoid permanent complexity and split-source behavior.
- Inline prune-on-write for commit-scoped tables:
  - Deferred; correctness does not require it, and batched compaction is operationally safer.

## References
- `docs/design-decisions/019-ci-by-sha-ledger-and-sha-keyed-ci.md`
- `docs/design-decisions/012-prrevision-head-changes.md`
- `docs/design-decisions/013-prrevision-incremental-build-state.md`
- `qb_site/syncer/models/commit_check_run.py`
- `qb_site/syncer/models/commit_status_context.py`
- `qb_site/syncer/services/sub/ci_sync.py`
- `qb_site/syncer/tasks/sync_tasks.py`
- `qb_site/analyzer/services/queue_windows.py`
- `qb_site/analyzer/services/queueboard_snapshot.py`
- `qb_site/analyzer/services/revisions.py`
- `qb_site/analyzer/tasks/plan_missing_ci.py`
