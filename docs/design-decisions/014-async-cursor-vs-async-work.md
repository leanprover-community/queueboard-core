# Async Cursors vs Downstream Async Work

## Context
- Several Syncer/Analyzer flows advance cursors or mark work “done” before downstream async tasks have actually completed:
  - `syncer.backfill_repo_history_task` persists `RepoBackfillCursor` after queuing `sync_pr_task` for discovered PR numbers.
- `syncer.harvest_commit_history_task` advances `CommitHistoryHarvest` cursors/has_more as soon as commit pages are read, then fires `sync_ci_for_shas_task` for SHAs with missing or only pending/queued CI.
  - `analyzer.process_pr` rebuilds revisions/windows and enqueues commit-history harvest jobs without waiting for those harvest/CI tasks.
- Failures in the enqueued tasks (or their downstream CI fetches) are not currently propagated back to rewind cursors or retry automatically; eventual consistency is relied upon via independent sweeps (e.g., commit-history sweep, missing-CI planners).

## Decision
- Accept the current optimistic cursor advancement and fire-and-forget enqueue pattern, and document it as a known source of transient inconsistency.
- Rely on secondary sweeps/backfills (history backfill, incomplete-PR backfill, commit-history sweep, future missing-CI sweeps) to converge rather than blocking cursor advancement on downstream task success.

## Consequences
- If a queued `sync_pr` or `sync_ci_for_shas` task fails after the cursor advanced, that specific work item may be missed until a separate sweep rediscovers it (or may never be retried if no sweep covers it).
- Operationally simpler and low-latency: cursors advance even when enqueue channels are degraded, keeping backfill progress moving.
- Requires awareness during incident response: data gaps can arise from downstream task failures without cursor rollback.

## Operational Notes
- Commit-history sweep (`syncer.harvest_commit_history_sweep`) retries only when `has_more=True`; once a harvest cursor completes, failed CI fetches are not retried automatically unless another planner enqueues them.
- History backfill cursor (`RepoBackfillCursor`) will not revisit pages even if some `sync_pr` tasks fail; incomplete-PR backfill and discovery may eventually resync such PRs.
- Future improvements to consider if this bites us:
  - Store harvested SHAs and retry CI fetches explicitly (or add a “missing CI” sweep keyed off stored SHAs).
  - Delay cursor advancement until enqueue succeeds (does not cover downstream task failures).
  - Add per-SHA attempt tracking with backoff/TTL to reduce silent skips.
