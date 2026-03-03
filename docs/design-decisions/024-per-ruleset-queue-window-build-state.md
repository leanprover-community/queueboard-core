# Per-Ruleset Queue Window Build State

## Context
- Queue windows are materialized per `(pull_request, rule_set)` in `analyzer.PRQueueWindow`.
- Freshness tracking was historically PR-level via `analyzer.PRRevisionBuildState.windows_built_revision_version` and `windows_built_at`.
- PR-level freshness caused avoidable work when only a subset of active rulesets changed.
- Convergence needed to represent stale window state at `(PR, ruleset)` granularity.

## Decision
- Build freshness is tracked per `(PR, ruleset)` using `analyzer.PRQueueWindowBuildState`.
- Queue-window sweep rebuilds only stale rulesets for each PR.
- Analyzer convergence computes `windows_stale` as stale `(PR, ruleset)` pairs.
- Transitional PR-level fallback reads were removed after production validation.
- Queue-window paths no longer write PR-level `windows_built_*`; per-ruleset state is the source of truth for freshness.
- PR-level fields remain in schema for compatibility/observability in this phase, but are no longer used by sweep/convergence freshness logic.

## Architecture

### Data Model
- `analyzer.PRQueueWindowBuildState` (unique on `(pull_request, rule_set)`) stores:
  - `revision_version_built`
  - `windows_built_at`
  - `last_status`
  - `last_reason`
- `analyzer.PRQueueWindow` remains the canonical materialized queue-window table.
- Build-state rows are metadata only; they do not replace `PRQueueWindow` contents.

### Staleness Semantics
A `(PR, ruleset)` pair is stale when any of the following are true:
- build-state row is missing,
- `revision_version_built` is null,
- `revision_version_built < PRRevisionBuildState.revision_version`,
- `windows_built_at` is null,
- `windows_built_at < QueueRuleSet.updated_at`,
- `queue_windows_need_rollup_backfill(pr, rule_set)` is true.

### Sweep Behavior (`analyzer.rebuild_queue_windows_sweep`)
- Sweep selects candidate PRs using a conservative prefilter over active rulesets:
  - missing state rows,
  - null per-ruleset freshness fields,
  - min revision/version lag,
  - min build timestamp older than max active ruleset update,
  - rollup-backfill signals.
- For each candidate PR, exact stale rulesets are computed with per-ruleset logic.
- Only stale rulesets are rebuilt via `rebuild_queue_windows_for_pr(pr, rule_sets=...)`.
- Sweep records/upserts per-ruleset build state for stale-attempted rulesets, including no-op rebuild attempts.

### Convergence Behavior (`analyzer.collect_convergence`)
- `windows_stale` counts stale `(PR, ruleset)` pairs across active rulesets.
- Missing per-ruleset build-state rows count as stale.
- Field name remains `windows_stale` for compatibility; semantics are per-pair, not PR-level.

### process_pr Behavior (`analyzer.tasks.process_pr`)
- When queue windows are rebuilt, `process_pr` records per-ruleset build-state updates.
- `process_pr` does not update PR-level `windows_built_*`.

### Backfill Path
- `backfill_queue_window_build_states` populates/realigns per-ruleset build-state rows for existing PRs and active rulesets.
- Dry-run mode is used for readiness and coverage checks.

## Consequences
- Pros:
  - Avoids rebuilding unaffected rulesets.
  - Makes convergence stale counts reflect real per-ruleset freshness.
  - Removes dependence on legacy PR-level freshness reads/writes in queue-window paths.
- Trade-offs:
  - More metadata rows (`O(PRs × active_rulesets)`).
  - Sweep prefilter is intentionally conservative (may include extra candidates) and exact filtering happens per PR in Python.
- Performance note:
  - The hardened aggregate-based prefilter (`Count/Min` with explicit null counts) reduced query time and buffer usage versus the prior correlated `Exists` shape in production analysis.

## Subtleties / Invariants
- Active-ruleset scope:
  - Sweep staleness decisions use active rulesets only.
- No-op rebuild behavior:
  - Per-ruleset build-state is still updated on stale-attempted no-op rebuilds to avoid endless rechecks.
- Bounds behavior:
  - Effective-from/effective-to bounds remain in queue-window builder semantics; build-state may record skip outcomes.
- Soundness boundary:
  - Convergence stale logic is exact per pair.
  - Sweep prefilter is approximate-by-design but must avoid false negatives; regression tests cover prior mismatch classes (subset stale, null field on one ruleset).

## Operational Notes
- Readiness checks before removing fallback used:
  - backfill dry-run with `rows_created=0`, and
  - coverage check of expected vs actual `(PR with revisions × active rulesets)` build-state rows.
- Post-deploy monitoring focuses on:
  - `analyzer.rebuild_queue_windows_sweep` summaries (`prs_checked`, `prs_stale_ruleset`, `windows_rebuilt`),
  - `analyzer.collect_convergence.windows_stale` trend,
  - missing-pair coverage regressions.
- Recovery path for anomalies:
  - targeted per-PR queue-window rebuild,
  - inspect/reconcile `PRQueueWindowBuildState` rows,
  - rerun convergence.

## References
- `qb_site/analyzer/models/pr_queue_window_build_state.py`
- `qb_site/analyzer/services/queue_window_build_state.py`
- `qb_site/analyzer/tasks/rebuild_queue_windows_sweep.py`
- `qb_site/analyzer/tasks/process_pr.py`
- `qb_site/analyzer/tasks/collect_convergence.py`
- `qb_site/analyzer/management/commands/backfill_queue_window_build_states.py`
- `qb_site/analyzer/tests/tasks/test_rebuild_queue_windows_sweep_task.py`
- `qb_site/analyzer/tests/tasks/test_process_pr.py`
- `qb_site/analyzer/tests/tasks/test_collect_convergence_task.py`
- `qb_site/analyzer/tests/services/test_queue_window_build_state.py`
