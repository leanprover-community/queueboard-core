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
  - `SYNCER_CI_SHA_STORAGE_DUAL_WRITE` (default `True` as of 2026-03-04)
  - `SYNCER_CI_PR_STORAGE_WRITE` (default `True`; set `False` for S6 PR-write retirement)
  - `ANALYZER_CI_SHA_READ_PRIMARY` (default `True` as of 2026-03-04)
  - `ANALYZER_CI_SHA_READ_FALLBACK_PR` (default `False` as of 2026-03-04)
- Keep the flags explicit rather than overloading existing backoff settings.

## Invariants / Subtleties
- Invariant: CI evaluation for a PR at a point in time must be against that revision's `head_sha`.
- Invariant: dual-write must not change externally visible queue/snapshot behavior before read cutover.
- Invariant: if SHA-keyed rows exist for a SHA, analyzer should prefer them consistently (avoid mixed-source per-context reads within a single evaluation).
- Invariant: backfill plus dual-write must converge to identical or stricter correctness vs PR-keyed source.
- Subtlety: multiple PRs can reference one SHA; SHA-keyed writes must not retain PR-specific metadata that changes semantics.
- Subtlety: provider IDs are not always complete for all context types, so dedupe identity must support provider-ID-present and provider-ID-absent cases.
- Subtlety: old SHAs may have no CI on GitHub due to retention; this remains handled by `CIShaFetchState` policy and is orthogonal to storage migration.
- Subtlety: commit-scoped tables may accumulate historical snapshot rows per
  context name unless we add explicit pruning/compaction; read logic currently
  enforces latest-wins semantics, but storage compaction should be planned.

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
   - Add commit-scoped snapshot pruning/compaction command(s):
     - keep latest row per `(repository, head_sha, normalized context name)` for
       GraphQL snapshot identities,
     - keep any required history rows only when explicitly needed by analytics.
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
- Cleanup/pruning tests:
  - latest-row retention per `(repository, head_sha, name)` for commit-scoped
    tables,
  - no regression in analyzer CI outcomes before vs after compaction.
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

### S4/S5 Flag Rollout
- Recommended flag sequence:
  1. `SYNCER_CI_SHA_STORAGE_DUAL_WRITE=1`
  2. Soak for at least one sync cycle and confirm commit-scoped row growth.
  3. `ANALYZER_CI_SHA_READ_PRIMARY=1` with `ANALYZER_CI_SHA_READ_FALLBACK_PR=1`
  4. Soak and validate queue/snapshot outputs against expected behavior.
  5. `ANALYZER_CI_SHA_READ_FALLBACK_PR=0` after confidence.
- Suggested checks during soak:
  - queueboard snapshot sanity (queue size and key dashboard lists stable vs expected),
  - queue-window rebuild spot checks on PRs with recent force-pushes,
  - no sustained growth in CI-mismatch/debug counters,
  - no backfill command conflicts (`skipped_conflict`) in follow-up runs.
- Fast rollback levers:
  - read regressions: set `ANALYZER_CI_SHA_READ_FALLBACK_PR=1` immediately,
  - broader read instability: set `ANALYZER_CI_SHA_READ_PRIMARY=0`,
  - ingest instability: set `SYNCER_CI_SHA_STORAGE_DUAL_WRITE=0`.
- Default updates as phases complete:
  - once dual-write is stable, change default for `SYNCER_CI_SHA_STORAGE_DUAL_WRITE`
    from `0` to `1` in settings/docs.
  - once SHA-primary reads with fallback are stable, change default for
    `ANALYZER_CI_SHA_READ_PRIMARY` from `0` to `1`.
  - once fallback-off is stable, change default for
    `ANALYZER_CI_SHA_READ_FALLBACK_PR` from `1` to `0`.
  - remove transitional flags entirely in `S7` after deprecation window.

### S3 Backfill Runbook
- Command:
  - `uv run python qb_site/manage.py backfill_sha_keyed_ci [options]`
- Recommended order:
  1. Run a dry-run first to confirm planned scope and output format.
  2. Run apply mode in bounded chunks using `--max-checkruns` and `--max-status-contexts`.
  3. Resume with returned `next_start_id` cursors until both models are fully scanned.
- Suggested first run:
  - `uv run python qb_site/manage.py backfill_sha_keyed_ci --dry-run --batch-size 1000 --max-checkruns 50000 --max-status-contexts 50000`
- Suggested apply run:
  - `uv run python qb_site/manage.py backfill_sha_keyed_ci --batch-size 1000 --max-checkruns 50000 --max-status-contexts 50000`
- Resume example:
  - If summary shows `"next_start_id": 123456` for `check_runs` and `234567` for `status_contexts`, continue with:
    - `uv run python qb_site/manage.py backfill_sha_keyed_ci --checkrun-start-id 123456 --status-start-id 234567 --batch-size 1000 --max-checkruns 50000 --max-status-contexts 50000`
- Progress behavior:
  - The command prints planned totals before writing:
    - `Planned rows: total=... (check_runs=..., status_contexts=...)`
  - It prints progress every 1000 processed rows:
    - `Progress: 1000/N rows processed`
- Interpreting summary counters:
  - `inserted`: new commit-scoped rows created.
  - `updated`: existing commit-scoped rows changed to match source.
  - `skipped_duplicate`: source row already represented with identical values.
  - `skipped_invalid`: source row missing required keys (e.g., missing provider ids or sha).
  - `skipped_conflict`: uniqueness conflict during upsert; investigate if non-trivial.
- Safety notes:
  - Backfill is idempotent and safe to re-run.
  - `--dry-run` executes logic and rolls back writes in one transaction.
  - Prefer running during lower write load for easier monitoring, though the operation is online-safe.

## Open Questions
- Exact identity constraints for SHA-keyed rows when provider IDs are absent/inconsistent.
- Whether to include a lightweight per-SHA CI completeness marker separate from `CIShaFetchState`.
- Whether old PR-keyed tables should be retained for audit/debug after cutover, and for how long.

## Progress Notes
- 2026-03-04:
  - `S5` soak outcome (operational):
    - SHA-primary reads have been running with PR fallback disabled for multiple
      hours in production without observed queue/snapshot regressions.
  - `S6` implementation (in progress):
    - Flipped code defaults to SHA-first operation:
      - `SYNCER_CI_SHA_STORAGE_DUAL_WRITE` default `True`
      - `ANALYZER_CI_SHA_READ_PRIMARY` default `True`
      - `ANALYZER_CI_SHA_READ_FALLBACK_PR` default `False`
    - Updated analyzer tests that intentionally use PR-keyed fixtures to set
      explicit per-class overrides, so they exercise queue/snapshot logic
      rather than depending on global storage defaults.
    - Added a dedicated ingest gate `SYNCER_CI_PR_STORAGE_WRITE` (default `True`)
      so PR-keyed writes can be disabled explicitly per environment.
    - Updated CI ingest (`sync_check_runs`/`sync_status_contexts`) so when
      PR-keyed writes are disabled, commit-scoped writes are still forced on
      even if dual-write is toggled off, preventing dropped CI data.
    - Updated analyzer revision CI consumers in
      `qb_site/analyzer/services/revisions.py` to include commit-scoped CI rows
      (`CommitCheckRun`/`CommitStatusContext`) keyed by
      `(repository, candidate_head_shas)` so revision rebuild and missing-CI
      planning continue to work after PR-keyed writes are turned off.
    - Updated pending-CI refresh task path in `qb_site/syncer/tasks/sync_tasks.py`
      to include commit-scoped CI rows for:
      - actionable pending detection on head SHA,
      - missing-head-CI detection,
      so `syncer.refresh_pending_ci_for_repo` remains effective with
      `SYNCER_CI_PR_STORAGE_WRITE=0`.
    - Added `syncer` task tests for commit-scoped-only pending/head-context
      behavior in `qb_site/syncer/tests/tasks/test_refresh_pending_ci_task.py`.
    - Added syncer coverage for PR-write-disabled mode in
      `qb_site/syncer/tests/subsystems/test_ci_sync.py`.
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
  - Implemented initial `S2` dual-write wiring:
    - Added setting `SYNCER_CI_SHA_STORAGE_DUAL_WRITE` (default `False`).
    - Added commit-scoped upserts inside shared CI ingest helpers in
      `qb_site/syncer/services/sub/ci_sync.py`, so both PR-bundle ingest and
      CI-by-SHA ingest paths dual-write when enabled.
    - Added `syncer` subsystem tests covering dual-write disabled/enabled paths.
  - Implemented `S3` backfill command/service:
    - Added backfill service `qb_site/syncer/services/ci_storage_backfill.py`
      with idempotent upsert counters for inserted/updated/skipped outcomes.
    - Added command `qb_site/manage.py backfill_sha_keyed_ci` with:
      - dry-run transaction rollback,
      - resumable cursors (`--checkrun-start-id`, `--status-start-id`),
      - bounded runs (`--max-checkruns`, `--max-status-contexts`, `--batch-size`),
      - optional repo scoping (`--repo owner/name`).
    - Added tests for backfill logic and command behavior:
      - `qb_site/syncer/tests/services/test_ci_storage_backfill.py`
      - `qb_site/syncer/tests/management/test_backfill_sha_keyed_ci_cmd.py`
    - Command UX improvement:
      - prints planned total rows before processing,
      - prints progress every 1000 processed rows by default, including
        total + per-model counters (`check_runs`, `status_contexts`).
    - Robustness fix:
      - backfill now handles provider-identity uniqueness conflicts without
        aborting the whole run (falls back to alternate unique identities).
    - Freshness fix:
      - backfill updates are now freshness-aware (timestamp-based), so older
        source rows encountered later do not overwrite newer commit-scoped CI data.
  - Implemented initial `S4` analyzer dual-read wiring:
    - Added settings flags:
      - `ANALYZER_CI_SHA_READ_PRIMARY` (default `False`)
      - `ANALYZER_CI_SHA_READ_FALLBACK_PR` (default `True`)
    - Updated analyzer CI reads to prefer commit-scoped tables when enabled:
      - `queue_windows`: `_latest_ci_statuses_for_fragment` and CI-gated queue-window builder path.
      - `queueboard_snapshot`: `_ci_inputs_for_repo` now sources SHA-keyed CI rows by
        `(repository, head_sha)` and maps them to PRs by resolved head SHA.
    - Added tests covering SHA-primary and fallback behavior:
      - `qb_site/analyzer/tests/services/test_queue_windows_prrevision.py`
      - `qb_site/analyzer/tests/test_queueboard_snapshot.py`
  - Production hardening follow-up:
    - Fixed dual-write ingest conflict handling in `qb_site/syncer/services/sub/ci_sync.py`
      so `syncer.sync_pr` does not fail when `github_node_id` upsert collides with
      existing `(repository, head_sha, name, external_id)` uniqueness.
    - Added regression coverage in
      `qb_site/syncer/tests/subsystems/test_ci_sync.py`.

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
