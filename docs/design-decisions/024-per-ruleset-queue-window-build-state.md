# Per-Ruleset Queue Window Build State (Living Plan)

## Context
- Queue windows are already materialized per `(pull_request, rule_set)` in `analyzer.PRQueueWindow`.
- At plan start, build freshness was tracked only once per PR in `analyzer.PRRevisionBuildState`:
  - `windows_built_revision_version`
  - `windows_built_at`
- At plan start, rebuild and convergence logic were PR-level:
  - Sweep (`qb_site/analyzer/tasks/rebuild_queue_windows_sweep.py`) rebuilds all active
    rulesets for a PR when any ruleset appears stale.
  - Convergence (`qb_site/analyzer/tasks/collect_convergence.py`) computes `windows_stale`
    against PR-level window build fields plus max ruleset `updated_at`.
- This produces avoidable recomputation when only one ruleset changed or was added.

## Goals / Non-Goals
- Goals
  - Track queue-window build freshness per `(PR, ruleset)`.
  - Rebuild only stale rulesets for each PR during sweeps.
  - Make convergence reporting match per-ruleset freshness reality.
  - Preserve behavior during rollout with safe fallback, then remove fallback after validation.
- Non-goals
  - Changing queue window semantics or CI gating semantics in this decision.
  - Dropping `PRRevisionBuildState.windows_built_*` schema in this phase.
  - Reworking CI storage architecture (covered by `019` Part 2).

## Proposed Design
- Add model `analyzer.PRQueueWindowBuildState`:
  - `pull_request` (FK to `syncer.PullRequest`)
  - `rule_set` (FK to `analyzer.QueueRuleSet`)
  - `revision_version_built` (int, nullable)
  - `windows_built_at` (datetime, nullable)
  - `last_status` (string, nullable; diagnostic)
  - `last_reason` (string, nullable; diagnostic)
  - unique constraint on `(pull_request, rule_set)`
- Introduce per-ruleset stale check logic used by sweep and convergence:
  - Missing row => stale
  - `revision_version_built` missing or `< PRRevisionBuildState.revision_version` => stale
  - `windows_built_at` missing or `< QueueRuleSet.updated_at` => stale
  - `queue_windows_need_rollup_backfill(pr, rule_set)` => stale
- Sweep behavior update:
  - For each PR, compute stale rulesets.
  - Rebuild only stale rulesets via `rebuild_queue_windows_for_pr(pr, rule_sets=stale_rulesets)`.
  - Upsert/update `PRQueueWindowBuildState` rows for rebuilt/stale-attempted rulesets.
- Convergence behavior update:
  - Move `windows_stale` accounting to stale `(PR, ruleset)` pairs.
  - Keep `AnalyzerConvergenceSnapshot.windows_stale` field name for compatibility,
    but document new meaning.
- Rollout fallback (transitional, now removed):
  - During transition, per-ruleset-missing state used legacy PR-level fields.
  - After post-deploy validation, fallback reads were removed in sweep/convergence.

## Subtleties / Invariants
- Invariant: `PRQueueWindow` remains the source of materialized windows; build-state
  rows are metadata only.
- Invariant: per-ruleset state updates should happen even when rebuild is a no-op,
  mirroring current `windows_built_at` behavior that prevents endless rechecks.
- Invariant: staleness checks only consider active rulesets for sweep decisions.
- Invariant: effective-from/effective-to out-of-bounds handling stays in window builder;
  build-state records may still be updated with a skip reason.

## Implementation Plan (Chunks)
1. Schema + write path bootstrap
   - Add `PRQueueWindowBuildState` model + migration.
   - Export/admin registration and basic indexes/constraints.
   - Add helper(s) to upsert per-ruleset state rows.
2. Sweep read/write migration
   - Update `rebuild_queue_windows_sweep_task` to compute stale rulesets per PR.
   - Rebuild only stale subsets.
   - Continue updating PR-level `windows_built_*` during transition.
3. process_pr alignment
   - Update `analyzer.tasks.process_pr` to write per-ruleset build state after rebuild.
   - Keep existing PR-level writes for compatibility during rollout.
4. Convergence migration
   - Update `collect_analyzer_convergence_task` to compute stale window counts from
     per-ruleset state (with transitional fallback).
5. Backfill + cleanup
   - Add a targeted backfill path for existing `(PR, active_ruleset)` pairs.
   - After stability, reduce/remove PR-level `windows_built_*` dependence in sweep/convergence.
6. Post-rollout hardening
   - Validate sweep prefilter soundness against convergence stale semantics.
   - Add regression tests for prefilter false-negative classes.
   - Remove PR-level `windows_built_*` writes from sweep/process_pr.

## Validation Plan
- Tests to add/update:
  - `qb_site/analyzer/tests/tasks/test_rebuild_queue_windows_sweep_task.py`
    - only stale rulesets are rebuilt
    - non-stale rulesets are skipped
    - no-op rebuild still updates per-ruleset build state
  - `qb_site/analyzer/tests/tasks/test_process_pr.py`
    - per-ruleset state written during rebuild path
  - `qb_site/analyzer/tests/tasks/test_collect_convergence_task.py`
    - stale window counts reflect stale `(pr, ruleset)` pairs
- Commands:
  - `uv run ruff format qb_site`
  - `uv run ruff check qb_site`
  - `uv run python qb_site/manage.py test analyzer.tests.tasks.test_rebuild_queue_windows_sweep_task`
  - `uv run python qb_site/manage.py test analyzer.tests.tasks.test_process_pr`
  - `uv run python qb_site/manage.py test analyzer.tests.tasks.test_collect_convergence_task`

## Progress Notes
- 2026-03-02:
  - Re-validated current implementation:
    - `019` Part 1 is implemented (`CIShaFetchState` + backoff policy/settings + task integration).
    - `019` Part 2 is not implemented yet.
    - `023` is not implemented yet (still boolean `require_ci_success` semantics).
  - Confirmed `024` is still pending and should be implemented before `023`.
  - Converted this document into a living plan and began chunked implementation.
  - Chunk 1 implementation completed:
    - Added `PRQueueWindowBuildState` model and migration:
      - `qb_site/analyzer/models/pr_queue_window_build_state.py`
      - `qb_site/analyzer/migrations/0022_prqueuewindowbuildstate.py`
    - Added write-path helper:
      - `qb_site/analyzer/services/queue_window_build_state.py`
    - Hooked write path in sweep:
      - `qb_site/analyzer/tasks/rebuild_queue_windows_sweep.py`
    - Added admin visibility:
      - `qb_site/analyzer/admin.py`
    - Added targeted test coverage for state-row writes:
      - `qb_site/analyzer/tests/tasks/test_rebuild_queue_windows_sweep_task.py`
    - Validation status:
      - Targeted `ruff check` on changed files passed.
      - Django test execution in this environment is currently blocked by missing Postgres.
  - Chunk 2 implementation completed:
    - Updated sweep task to compute stale rulesets per PR and rebuild only stale subsets:
      - `qb_site/analyzer/tasks/rebuild_queue_windows_sweep.py`
    - Added transitional fallback behavior when per-ruleset state rows are missing:
      - uses legacy `PRRevisionBuildState.windows_built_*` checks until per-ruleset rows exist.
    - Expanded sweep tests for selective rebuild and edge cases:
      - `qb_site/analyzer/tests/tasks/test_rebuild_queue_windows_sweep_task.py`
    - Validation status:
      - Targeted `ruff check` on changed files passed.
      - Django test execution in this environment is currently blocked by missing Postgres.
  - Chunk 3 implementation completed:
    - Process-PR per-ruleset state write alignment (landed during early scaffolding, tracked here by ownership):
      - `qb_site/analyzer/tasks/process_pr.py`
    - Added/updated process-PR test coverage for per-ruleset state writes:
      - `qb_site/analyzer/tests/tasks/test_process_pr.py`
    - Validation status:
      - Targeted `ruff check` on changed files passed.
      - Django test execution in this environment is currently blocked by missing Postgres.
  - Chunk 4 implementation completed:
    - Convergence migration to per-(PR, ruleset) stale accounting with transitional fallback:
      - `qb_site/analyzer/tasks/collect_convergence.py`
    - Added convergence test coverage for per-ruleset stale pair counting:
      - `qb_site/analyzer/tests/tasks/test_collect_convergence_task.py`
    - Validation status:
      - Targeted `ruff check` on changed files passed.
      - Django test execution in this environment is currently blocked by missing Postgres.
  - Chunk 5 implementation completed:
    - Added a targeted backfill path for per-ruleset build state:
      - Service helper: `qb_site/analyzer/services/queue_window_build_state.py`
      - Management command: `qb_site/analyzer/management/commands/backfill_queue_window_build_states.py`
    - Added tests for service and command behavior:
      - `qb_site/analyzer/tests/services/test_queue_window_build_state.py`
      - `qb_site/analyzer/tests/management/test_backfill_queue_window_build_states_cmd.py`
    - Validation status:
      - `uv run ruff format qb_site` executed.
      - Targeted `ruff check` on changed files passed.
      - Django test execution in this environment is currently blocked by missing Postgres.
  - Post-implementation verification from user run:
    - DB-backed tests for touched analyzer paths were run by user and reported green.
  - Related stability fix (outside direct 024 scope, but relevant to observed churn):
    - Updated CI sync dirty-marking so unchanged CI snapshot re-observations do not repeatedly
      set `PRRevisionBuildState.dirty_from_ts`.
    - Refined CI dirty-marking to ignore stale historical rows when a payload contains multiple
      contexts for the same CI name; only the newest row per name contributes to dirtying.
    - Files:
      - `qb_site/syncer/services/sub/ci_sync.py`
      - `qb_site/syncer/tests/subsystems/test_ci_sync.py`
- 2026-03-03:
  - Production verification for fallback removal readiness:
    - Backfill dry-run on active repo showed `rows_created=0` (coverage complete).
    - Coverage check for `(timeline_backfill_done + has revisions) × active rulesets` reported `missing=0`.
  - Removed transitional fallback reads:
    - Sweep no longer uses PR-level `windows_built_*` fallback when per-ruleset row is missing.
    - Convergence no longer uses PR-level fallback for missing per-ruleset rows.
    - Files:
      - `qb_site/analyzer/tasks/rebuild_queue_windows_sweep.py`
      - `qb_site/analyzer/tasks/collect_convergence.py`
  - Investigated stuck stale pair report (`windows_stale` remained at 2 while sweep summaries showed `prs_checked=0`):
    - Identified stale pairs on one PR/ruleset pair set in production.
    - Manual one-off rebuild cleared the stale pairs (`windows_stale` dropped to 0).
    - Hypothesis: prefilter selection mismatch risk under correlated `Exists(...)` shape.
  - Prefilter hardening and regression coverage:
    - Replaced correlated stale-state prefilter with aggregate-based candidate signals (`Count/Min`) plus explicit null-state counts.
    - Added regression tests to cover:
      - stale subset selected by prefilter,
      - one-ruleset null `revision_version_built`,
      - one-ruleset null `windows_built_at`.
    - Files:
      - `qb_site/analyzer/tasks/rebuild_queue_windows_sweep.py`
      - `qb_site/analyzer/tests/tasks/test_rebuild_queue_windows_sweep_task.py`
  - Performance validation (production EXPLAIN ANALYZE, same dataset):
    - Old prefilter: ~1.44s execution, ~995k shared buffer hits.
    - New prefilter: ~0.33s execution, ~255k shared buffer hits.
    - Conclusion: new prefilter is materially faster and lower-IO while avoiding prior false-negative risk class.
  - Removed PR-level `windows_built_*` writes from queue-window paths:
    - Sweep no longer writes PR-level window freshness fields.
    - `process_pr` no longer writes PR-level window freshness fields.
    - Per-ruleset writes via `PRQueueWindowBuildState` remain the source of freshness truth.
    - Files:
      - `qb_site/analyzer/tasks/rebuild_queue_windows_sweep.py`
      - `qb_site/analyzer/tasks/process_pr.py`
      - `qb_site/analyzer/tests/tasks/test_rebuild_queue_windows_sweep_task.py`
      - `qb_site/analyzer/tests/tasks/test_process_pr.py`
  - Validation status:
    - `ruff check`/`ruff format` clean on touched files.
    - DB-backed targeted tests for updated analyzer task modules reported green by user.

## Finalization Notes
- After implementation stabilizes, condense this file into a concise final decision
  record and document final semantics/metrics references.
- Current status:
  - Core implementation is complete through post-rollout hardening.
  - Remaining follow-up is optional schema cleanup/deprecation of PR-level fields in a future decision.

## Operational Notes (Deploy + Backfill)
- Recommended rollout order:
  1. Deploy application code + migrations (includes `PRQueueWindowBuildState` table).
  2. Run a dry-run backfill on one repository.
  3. Run write-mode backfill on one repository.
  4. Run queue-window sweep for that repository cohort and check convergence/admin output.
  5. Expand to remaining repositories.
- Backfill command:
  - Dry-run:
    - `uv run python qb_site/manage.py backfill_queue_window_build_states --repo <owner>/<name>`
  - Write mode:
    - `uv run python qb_site/manage.py backfill_queue_window_build_states --repo <owner>/<name> --write`
  - Scoped run for specific PRs:
    - `uv run python qb_site/manage.py backfill_queue_window_build_states --repo <owner>/<name> --pr 123 456 --write`
- Suggested execution pattern:
  - First pass: dry-run all active repositories and record `prs_considered`, `rows_created`, `rows_updated`.
  - Second pass: run `--write` for the same set.
  - Third pass: trigger queue-window sweep to refresh stale subsets after state row creation.
- Validation checks after each repository:
  - `analyzer.PRQueueWindowBuildState` row count should approach:
    - open/timeline-complete PR count × active ruleset count (allowing temporary lag).
  - `analyzer.rebuild_queue_windows_sweep` task summaries should show selective stale-ruleset rebuilds.
  - `analyzer.collect_convergence` should continue to run; note `windows_stale` now reflects stale
    `(PR, ruleset)` pairs (not only PR-level staleness).
- Safety/rollback notes:
  - If issues occur, pause backfill/sweeps and investigate; targeted per-PR rebuild + per-ruleset state inspection remains the primary recovery path.
  - No destructive data migration is required for this phase.
