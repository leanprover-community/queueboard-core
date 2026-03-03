# SHA-Keyed CI Storage Migration (Living Plan)

## Context
- `docs/design-decisions/019-ci-by-sha-ledger-and-sha-keyed-ci.md` split CI work into:
  - Part 1: CI-by-SHA backoff ledger (implemented as of 2026-03-02).
  - Part 2: migrate CI persistence/reads from PR-keyed to SHA-keyed storage (not implemented).
- Current persisted CI models are PR-keyed:
  - `syncer.CheckRun` (`qb_site/syncer/models/check_run.py`)
  - `syncer.StatusContext` (`qb_site/syncer/models/status_context.py`)
- Current analyzer CI reads are PR-keyed:
  - `analyzer.services.queue_windows._latest_ci_statuses_for_fragment`
  - `analyzer.services.queueboard_snapshot._ci_status_for_pr`
- CI fetch operations are already commit-SHA based in multiple paths (`sync_ci_for_sha`, pending CI refresh, commit-history CI planning).

## Problem Statement
- CI facts are fundamentally commit-scoped, but storage and many reads are still PR-scoped.
- This mismatch introduces ambiguity when SHAs move across PR revisions and when SHAs are shared between PRs.
- We need a safe, staged migration to SHA-keyed CI storage without destabilizing queue-window and snapshot correctness.

## Goals / Non-Goals
- Goals:
  - Make SHA-keyed CI tables the source of truth for analyzer CI evaluation.
  - Preserve behavior during migration via explicit dual-write and dual-read phases.
  - Keep rollback straightforward at each phase boundary.
  - Maintain existing backoff-ledger semantics (`CIShaFetchState`) and integrate with new tables.
- Non-goals:
  - Reworking CI context normalization/ruleset semantics in this effort.
  - Replacing `PRRevision` timeline semantics.
  - Immediate deletion of existing PR-keyed CI tables.

## Proposed Design

### New commit-scoped CI models
- Add new models under `qb_site/syncer/models/`:
  - `CommitCheckRun`
  - `CommitStatusContext`
- Shared identity key:
  - `(repository, head_sha)` plus provider/context identity fields.
- Suggested core fields:
  - `repository` FK
  - `head_sha` (char(64), indexed with repository)
  - normalized context/run name fields (`name`, and optional display variants)
  - state fields (`status`/`conclusion` for checks; `state` for status contexts)
  - provider IDs (`check_run_id`, `context_id`, etc. as available)
  - provider timestamps (`started_at`, `completed_at`, `created_at` where available)
  - `last_synced_at`
- Constraints and indexes (finalized in implementation chunk):
  - Unique provider-ID constraints where provider IDs are stable.
  - Composite uniqueness to avoid duplicate logical contexts when provider IDs are missing.
  - Lookup index on `(repository, head_sha)`.

### Write path strategy
- Migration writes are phased:
  1. Keep PR-keyed writes unchanged initially.
  2. Add dual-write: write SHA-keyed rows in all ingest paths that currently write PR-keyed CI rows.
  3. After read cutover and confidence period, disable PR-keyed writes.
- Primary ingest touchpoints:
  - `syncer.services.pr_sync_service`
  - `syncer.services.ci_by_sha_service`
  - Any helper used by both `sync_pr` and `sync_ci_for_shas` paths.

### Read path strategy
- Analyzer reads move in two steps:
  1. Dual-read with fallback:
     - Read SHA-keyed first.
     - If no SHA-keyed rows for `(repo, head_sha)`, optionally fall back to current PR-keyed rows.
  2. SHA-only reads after backfill + dual-write soak.
- Primary read touchpoints:
  - `qb_site/analyzer/services/queue_windows.py`
  - `qb_site/analyzer/services/queueboard_snapshot.py`

### Backfill strategy
- One-time/backfillable migration job to populate SHA-keyed tables from existing PR-keyed rows.
- Candidate approach:
  - Iterate existing `CheckRun`/`StatusContext` rows in batches.
  - Upsert into `CommitCheckRun`/`CommitStatusContext` keyed by repository+sha+identity.
  - Track counters: scanned, inserted, updated, skipped-duplicate, skipped-invalid.
- The backfill should be resumable and idempotent.

### Migration feature flags/settings
- Add temporary settings to gate phases cleanly:
  - `SYNCER_CI_SHA_STORAGE_DUAL_WRITE` (default `False`)
  - `ANALYZER_CI_SHA_READ_PRIMARY` (default `False`)
  - `ANALYZER_CI_SHA_READ_FALLBACK_PR` (default `True` during transition)
- Keep the flags explicit rather than overloading existing backoff settings.

## Invariants / Subtleties
- Invariant: CI evaluation for a PR at a point in time must be against that revision's `head_sha`.
- Invariant: dual-write must not change externally visible queue/snapshot behavior before read cutover.
- Invariant: if SHA-keyed rows exist for a SHA, analyzer should prefer them consistently (avoid mixed-source per-context reads within a single evaluation).
- Invariant: backfill plus dual-write must converge to identical or stricter correctness vs PR-keyed source.
- Subtlety: multiple PRs can reference one SHA; SHA-keyed writes must not retain PR-specific metadata that changes semantics.
- Subtlety: provider IDs are not always complete for all context types, so dedupe identity must support provider-ID-present and provider-ID-absent cases.
- Subtlety: old SHAs may have no CI on GitHub due to retention; this remains handled by `CIShaFetchState` policy and is orthogonal to storage migration.

## Phase Plan (Chunked)
1. `S1` Schema + model scaffolding.
   - Add `CommitCheckRun` and `CommitStatusContext` models, migrations, admin registration.
   - Add tests for constraints/index behavior and basic CRUD.
2. `S2` Write-path abstraction and dual-write implementation.
   - Introduce shared CI persistence helper(s) used by both PR sync and CI-by-SHA sync.
   - Gate SHA writes behind `SYNCER_CI_SHA_STORAGE_DUAL_WRITE`.
   - Keep PR writes unchanged.
3. `S3` Backfill command/task.
   - Add management command (and optional Celery task wrapper) for batched idempotent backfill.
   - Emit dry-run and execution summaries.
4. `S4` Analyzer dual-read cutover.
   - Add SHA-primary read path in queue-window and snapshot services.
   - Preserve optional fallback (`ANALYZER_CI_SHA_READ_FALLBACK_PR`).
   - Add tests proving parity in representative cases.
5. `S5` Soak + metrics-based validation.
   - Run dual-write + SHA-primary/fallback-on for a soak window.
   - Track mismatch counters between SHA and PR read outcomes.
6. `S6` Finalize SHA-only reads and retire PR-keyed writes.
   - Disable PR fallback.
   - Disable PR-keyed CI writes.
   - Keep legacy tables read-only during a deprecation window.
7. `S7` Cleanup migration.
   - Remove transitional flags and dead code.
   - Optionally drop old PR-keyed CI tables in a separate safety-reviewed change.

## Validation Plan
- Unit/model tests:
  - SHA-keyed model uniqueness and merge/upsert behavior.
  - Persistence helpers for provider-ID and name-based dedupe paths.
- Syncer task/service tests:
  - `sync_pr` and `sync_ci_for_shas` both write SHA-keyed rows when dual-write enabled.
  - No regressions when dual-write disabled.
- Analyzer tests:
  - Queue-window required-context evaluation parity for:
    - single SHA/single PR
    - shared SHA across two PRs
    - force-push SHA change between revisions
  - Snapshot CI status parity under dual-read.
- Backfill tests:
  - idempotent rerun behavior.
  - resume behavior from partial progress.
- Local checks:
  - `uv run ruff check qb_site`
  - `uv run ruff format qb_site`
  - targeted Django tests for `syncer` and `analyzer`.
  - full `bash scripts/repo_check_compose.sh` when available.

## Rollout / Operational Notes
- Recommended rollout sequence:
  1. Deploy `S1` (no behavior change).
  2. Deploy `S2` with dual-write off; then enable dual-write per environment.
  3. Run `S3` backfill to near-complete coverage.
  4. Deploy `S4` and enable SHA-primary with PR fallback on.
  5. After soak, disable PR fallback and disable PR writes (`S6`).
- Add temporary observability counters:
  - SHA write counts by source path.
  - SHA-read hit/miss and PR-fallback usage.
  - SHA-vs-PR evaluation mismatch counters (sampled if needed).
- Rollback policy:
  - If regressions appear after read cutover, keep dual-write on and re-enable PR fallback immediately.
  - If ingest regressions appear, disable dual-write flag while keeping schema in place.

## Open Questions
- Exact identity constraints for SHA-keyed rows when provider IDs are absent/inconsistent.
- Whether to include a lightweight per-SHA CI completeness marker separate from `CIShaFetchState`.
- Whether old PR-keyed tables should be retained for audit/debug after cutover, and for how long.

## Progress Notes
- 2026-03-03:
  - Created this living plan to break Part 2 of decision `019` into executable phases.
  - No migration code implemented yet.
  - Implemented `S1` schema/model scaffolding:
    - Added `syncer.CommitCheckRun` and `syncer.CommitStatusContext`.
    - Added migration `syncer/0030_commitcheckrun_commitstatuscontext.py`.
    - Added read-only admin views and initial model tests in `qb_site/syncer/tests/models/test_commit_ci_models.py`.
  - Current `S1` schema details:
    - Both models index `(repository, head_sha)` for SHA-scoped lookup.
    - `CommitCheckRun` has conditional uniqueness on `github_node_id` and on
      `(repository, head_sha, name, external_id)` when `external_id` is present.
    - `CommitStatusContext` has conditional uniqueness on `github_node_id` and `rest_id`.

## References
- `docs/design-decisions/019-ci-by-sha-ledger-and-sha-keyed-ci.md`
- `docs/design-decisions/012-prrevision-head-changes.md`
- `docs/design-decisions/013-prrevision-incremental-build-state.md`
- `qb_site/syncer/models/check_run.py`
- `qb_site/syncer/models/status_context.py`
- `qb_site/syncer/services/ci_by_sha_service.py`
- `qb_site/syncer/services/ci_backoff.py`
- `qb_site/analyzer/services/queue_windows.py`
- `qb_site/analyzer/services/queueboard_snapshot.py`
- `qb_site/analyzer/tasks/plan_missing_ci.py`
