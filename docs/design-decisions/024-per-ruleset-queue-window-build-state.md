# Per-Ruleset Queue Window Build State

## Context
- Queue windows are stored per `(pull_request, rule_set)` in `analyzer.PRQueueWindow`, but build progress metadata is currently tracked only once per PR in `analyzer.PRRevisionBuildState`:
  - `windows_built_revision_version`
  - `windows_built_at`
- Sweep rebuild logic currently treats staleness as a PR-level boolean:
  - if any active ruleset appears stale, it rebuilds queue windows for all active rulesets for that PR.
- This causes avoidable work when:
  - a new ruleset is added, or
  - one existing ruleset is edited.
- Relevant implementation paths:
  - `qb_site/analyzer/tasks/rebuild_queue_windows_sweep.py`
  - `qb_site/analyzer/services/queue_windows.py`
  - `qb_site/analyzer/tasks/collect_convergence.py`

## Decision
- Introduce per-(PR, ruleset) queue-window build state and use it as the source of truth for staleness and convergence.
- Add a new Analyzer model (name provisional): `PRQueueWindowBuildState` with:
  - `pull_request` (FK)
  - `rule_set` (FK)
  - `revision_version_built` (int, nullable)
  - `windows_built_at` (datetime, nullable)
  - optional metadata for diagnostics (`last_status`, `last_reason`)
  - unique constraint on `(pull_request, rule_set)`
- Change queue-window sweep to:
  - compute stale rulesets per PR,
  - rebuild only stale rulesets for that PR,
  - update state rows only for rebuilt rulesets.
- Keep `PRRevisionBuildState` focused on revision/CI planning state, not per-ruleset queue-window freshness.

## Consequences
- Pros
  - Adding a new ruleset rebuilds only that ruleset, not all existing ones.
  - Editing one ruleset invalidates only that ruleset’s build state.
  - Better observability: explicit build state per ruleset aligns with how windows are stored.
  - Better foundation for future ruleset semantics changes (including CI mode variants).
- Cons
  - Additional table and migration complexity.
  - Sweep and convergence logic become more complex.
  - Transitional period requires dual-read/fallback logic while existing rows are backfilled.

## Operational Notes
- Staleness criteria for a `(pr, rule_set)` pair:
  - no `PRQueueWindowBuildState` row exists, or
  - `revision_version_built` is null or `< PRRevisionBuildState.revision_version`, or
  - `windows_built_at` is null or `< QueueRuleSet.updated_at`, or
  - `queue_windows_need_rollup_backfill(pr, rule_set)` is true.
- Sweep behavior:
  - For each PR, build `stale_rule_sets`.
  - If empty, skip PR.
  - If non-empty, call `rebuild_queue_windows_for_pr(pr, rule_sets=stale_rule_sets)`.
  - Update `PRQueueWindowBuildState` rows for those stale rulesets after rebuild attempt.
- Convergence behavior:
  - Replace global `ruleset_updated_at` + PR-level window staleness checks with per-ruleset state aggregation.
  - Report stale counts in terms of missing/stale `(pr, rule_set)` build-state rows.
- Backward compatibility and rollout:
  - Phase 1: add new model and write path.
  - Phase 2: read both old and new state, preferring new state when present.
  - Phase 3: backfill new state for existing `(pr, rule_set)` pairs.
  - Phase 4: remove PR-level window staleness dependence from sweep/convergence; keep old fields optional/legacy.

## Sequencing
- This decision should be implemented before introducing additional queue-rule semantics (for example, `no_required_failures` CI gating mode from `023-ci-gating-no-required-failures.md`).
- Rationale:
  - semantics experiments will likely involve adding/editing rulesets,
  - per-ruleset build state prevents those changes from repeatedly triggering unnecessary full-rule-set recomputation.

## Alternatives (Optional)
- Keep current PR-level build fields and accept extra recomputation
  - Rejected: unnecessary compute and poor observability as rulesets evolve.
- Extend `PRRevisionBuildState` with JSON per ruleset
  - Rejected: weak relational integrity, harder querying/indexing, and more brittle migrations.
- Track only per-repo ruleset freshness
  - Rejected: misses per-PR revision-version coupling; too coarse to be correct.
