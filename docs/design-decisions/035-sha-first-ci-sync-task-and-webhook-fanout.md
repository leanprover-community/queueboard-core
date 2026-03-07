# SHA-First CI Sync Task And Webhook Fanout (Living Plan)

## Context
- Current check-event webhook routing resolves PR numbers and enqueues one `syncer.sync_ci_for_shas` task per resolved PR.
- Task contract today is PR-scoped (`repo_id`, `number`, `shas`) even though CI storage and dedupe pressure are increasingly SHA-centric.
- This creates avoidable fanout and duplicate pressure when one SHA is associated with multiple PRs (or repeatedly re-associated).
- Dedupe in decision `030` mitigates this operationally, but does not remove the architectural PR-fanout coupling.

## Goals / Non-Goals
- Goals:
  - introduce a SHA-first CI sync execution path that does not require per-PR fanout at enqueue time.
  - reduce webhook check-event task multiplicity for the same `(repo, sha)` work.
  - preserve analyzer and PR refresh behavior where it is still needed.
- Non-goals:
  - removing all PR-aware workflows from Syncer in one pass.
  - broad redesign of Analyzer revision processing in this doc.

## Problem Statement
- We currently use PR as the primary task identity for CI refresh, but many CI signals are naturally commit/SHA-scoped.
- PR fanout inflates queue pressure and complicates observability:
  - one delivery can trigger multiple near-identical CI sync tasks.
- A SHA-first path should align enqueue identity with the underlying CI data identity.

## Proposed Design

### A) New SHA-First Task Contract
- Introduce a new task (name tentative): `syncer.sync_ci_for_repo_shas`.
- Inputs:
  - `repo_id`
  - `shas` (list)
  - optional execution knobs (`max_pages_per_sha`, `dry_run`, etc.)
- Behavior:
  - fetch/store CI by SHA without requiring a single PR number as entry key.
  - resolve affected PRs after CI ingest (if needed) for follow-up actions.

### B) Webhook Routing Shift
- For check-event webhooks:
  - route directly to SHA-first task keyed by `(repo, head_sha)` batch identity.
  - stop per-PR enqueue fanout in the webhook path.
- Keep pull_request-event routing unchanged initially.

### C) PR-Aware Follow-Up (Compatibility Layer)
- After SHA sync, compute impacted PR ids from Analyzer revision history (`analyzer.PRRevision`)
  so historical head-SHA associations are covered.
- Trigger analyzer/process follow-up in a bounded way:
  - either once per impacted PR,
  - or via a future batched analyzer API (follow-up option).

### D) Dedupe Alignment
- Enqueue dedupe identity for check-event path becomes naturally SHA-keyed:
  - `repo_id:max_pages_per_sha:sorted(shas)` (already compatible with current helper shape).
- Runtime dedupe and/or in-flight locking decisions can be layered separately.

## Invariants
- CI persistence correctness remains anchored by idempotent upsert logic.
- Redis or dedupe failures must remain fail-open (do not drop required sync work).
- Any PR follow-up fanout must be explicit and observable in task summaries.

## Implementation Plan (Chunks)
1. ✅ Introduce SHA-first task skeleton and shared CI-by-SHA execution helper extraction.
2. ✅ Add webhook check-event routing option flag to call SHA-first task path.
3. ✅ Implement impacted-PR resolution and follow-up analyzer enqueue behavior.
4. ✅ Add metrics:
   - webhook check delivery count,
   - SHA task enqueued count,
   - impacted PR fanout count.
5. Migrate default webhook check path to SHA-first; keep fallback switch.
6. Remove obsolete PR-fanout-only assumptions once stable.

## Validation Plan
- Unit tests:
  - SHA task executes CI sync for provided SHA list without PR number dependency.
  - impacted PR resolution is correct for open/closed/missing associations.
- Integration/task tests:
  - one check-event delivery enqueues one SHA-first task (not per-PR tasks).
  - analyzer follow-up still runs for affected PRs.
- Ops validation:
  - compare per-delivery enqueue multiplicity before/after.
  - monitor queue depth slope and freshness for CI updates.

## Open Questions
- Should analyzer follow-up remain per-PR task fanout initially, or move to batched processing now?
- Should pending-CI refresh and commit-history CI producers also migrate to SHA-first immediately or in later phases?
- Do we keep `syncer.sync_ci_for_shas` as a compatibility wrapper, or fully replace it?

## Progress Notes
- 2026-03-06:
  - Split from decision `030-sync-task-dedupe-strategy.md` after identifying that webhook check-event fanout remains PR-scoped despite SHA-keyed CI needs.
  - This document now owns the architectural shift to SHA-first CI tasking; decision `030` remains focused on dedupe strategy and rollout.
  - Chunk 1 implemented:
    - Added shared runner `syncer/services/ci_sha_task_runner.py` to execute CI-by-SHA loops with common budget/defer behavior.
    - Refactored existing `syncer.sync_ci_for_shas` task to call the shared runner (no contract change).
    - Added new `syncer.sync_ci_for_repo_shas` task skeleton that accepts `(repo_id, shas)` and resolves impacted PRs internally.
    - Added/updated tests in `qb_site/syncer/tests/tasks/test_sync_ci_for_shas_task.py` for PR-scoped and repo-scoped entrypoints.
  - Chunk 2 implemented:
    - Added flag `SYNCER_GITHUB_WEBHOOK_CHECK_SHA_FIRST` (default `False`) for check-event routing.
    - With flag enabled, check-event webhooks enqueue one repo+SHA task (`syncer.sync_ci_for_repo_shas`) instead of per-PR fanout.
    - Added webhook endpoint test coverage for `sha_first` vs legacy `pr_fanout` mode summaries.
  - Clarified analyzer fanout source:
    - No new SHA↔PR mapping table is required for current scope.
    - Chunk 3 will resolve impacted PRs using `analyzer.PRRevision` (historical head-SHA coverage), then fan out analyzer tasks per PR initially.
  - Chunk 3 implemented:
    - `syncer.sync_ci_for_repo_shas` now resolves impacted PRs via `analyzer.PRRevision` for historical head-SHA coverage.
    - Added open-head fallback (`syncer.PullRequest.head_sha`) so recently-updated PRs are still included before revision rebuild catches up.
    - Analyzer follow-up remains per-PR fanout (`analyzer.process_pr`) for impacted PR ids.
    - Added task test coverage for PRRevision-driven impacted PR resolution.
  - Chunk 4 implemented:
    - Extended `SyncerMetricsSnapshot` with:
      - `webhook_check_deliveries`,
      - `webhook_sha_first_tasks_enqueued`,
      - `sha_task_impacted_pr_fanout_total`.
    - Updated `syncer.collect_metrics` aggregation:
      - check delivery count from webhook summaries (`route=check`),
      - SHA-first enqueue count from webhook summaries (`check_sync_mode=sha_first`, `reason=enqueued_sync_ci`),
      - impacted PR fanout total from `syncer.sync_ci_for_repo_shas` task results (`impacted_pr_count`).
    - Added migration and metrics test updates for the new fields.
  - Performance follow-up deferred:
    - Measured `EXPLAIN ANALYZE` for the `PRRevision` head-SHA lookup showed a seq scan over ~168k rows (~26ms in the sampled environment).
    - We are deferring the index change for now; include this in the final architecture record as a tracked follow-up.
    - Candidate future index: `analyzer_prrevision(head_sha, pull_request_id)` (or equivalent Django model index).
  - No additional design decision required before chunk 4.

## References
- `docs/design-decisions/030-sync-task-dedupe-strategy.md`
- `qb_site/syncer/views.py`
- `qb_site/syncer/tasks/sync_tasks.py`
- `qb_site/syncer/tasks/commit_history_tasks.py`
- `qb_site/syncer/services/ci_by_sha_service.py`
