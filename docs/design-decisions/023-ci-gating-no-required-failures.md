# CI Gating Mode: No Required Context Failures (Living Plan)

## Context
- Current ruleset CI gating is a boolean (`QueueRuleSet.require_ci_success`) with strict required-context semantics.
- Strict semantics are implemented in:
  - `qb_site/analyzer/services/queue_windows.py`
  - `qb_site/analyzer/services/queueboard_snapshot.py`
- Current ruleset model surface is in `qb_site/analyzer/models/queue_rule.py`.
- Backfill planning currently does not guarantee complete required-context observation per SHA (`qb_site/analyzer/services/revisions.py`).
- Operationally, some required jobs are intentionally not triggered for unaffected changes; strict semantics can keep those PRs off-queue forever.

## Problem Statement
- We need queue gating semantics that allow intentionally skipped/not-triggered required jobs without silently weakening existing strict behavior for other repositories.
- The implementation must keep queue-window materialization and snapshot inclusion semantics aligned.

## Goals / Non-Goals
- Goals:
  - Introduce explicit per-ruleset CI gating modes.
  - Preserve existing strict behavior as default.
  - Support a mode where missing/untriggered required contexts do not block queue eligibility.
  - Keep ruleset semantics explicit/auditable and versionable via existing ruleset lifecycle.
- Non-goals:
  - Replacing CI-by-SHA ingestion architecture from `019`.
  - Requiring full required-context coverage convergence in backfill planning for v1 rollout.
  - Removing required-context lists from rulesets.

## Proposed Design

### Ruleset Model Semantics
- Add explicit `ci_gating_mode` enum on `QueueRuleSet`:
  - `all_required_success` (default; current strict behavior)
  - `no_required_failures` (new behavior)
- Transitional compatibility:
  - Keep `require_ci_success` during migration.
  - Normalize call sites onto the new mode helper, then retire direct boolean checks in logic paths.

### Mode Semantics
- `all_required_success`:
  - Each required context must be observed and in a passing terminal state for the relevant head SHA/time.
  - Missing/unobserved/running contexts are not sufficient.
- `no_required_failures`:
  - Queue-eligible when no required context is observed failing for the relevant head SHA/time.
  - Missing/unobserved/not-triggered contexts are non-blocking.
  - Running/pending contexts are non-blocking unless they later fail.

### Queue Windows + Snapshot Alignment
- Queue windows:
  - Replace binary CI-ok helper usage with mode-aware CI state evaluation (`pass`/`fail`/`running`/`missing`).
  - Apply eligibility mapping by mode at boundary evaluation time.
- Snapshot:
  - Preserve coarse CI rollup surface (`pass`, `fail`, `running`, `missing`, `fail-inessential`).
  - Derive queue inclusion using the same mode mapping as queue windows.
  - Revisit "no windows => missing" shortcuts where no-fail semantics can legitimately have sparse windows.

### Backfill / Convergence Implications
- Under `no_required_failures`, complete required-context observation is not required for correctness.
- Critical risk shifts to failure ingestion misses; monitoring should emphasize false negatives for required failures.
- No mandatory planner redesign is needed in the first implementation chunk.

## Subtleties / Invariants
- Mode must be explicit per ruleset row; no time-based or heuristic fallback.
- Effective bounds/versioning remain the mechanism for semantics changes over time.
- Queue-window membership semantics and snapshot membership semantics must remain equivalent for a given `(PR, ruleset, time)`.
- Strict mode behavior must remain backwards compatible.
- `024` per-ruleset build-state freshness is now available and should be relied on for rollouts that touch ruleset semantics.

## Implementation Plan (Chunks)
1. Schema + model surface
- Add `ci_gating_mode` to `QueueRuleSet` with strict default.
- Add shared mode helper(s) so callers do not open-code boolean + enum fallback logic.
- Add migration/tests for defaulting and backwards compatibility.

2. Queue-window mode-aware gating
- Refactor CI evaluation path to expose explicit state, then map eligibility by mode.
- Keep strict behavior identical to current output for unchanged rulesets.
- Add focused tests for:
  - strict + missing required context => ineligible,
  - no-fail + missing required context => eligible,
  - no-fail + running required context => eligible,
  - no-fail + observed required failure => ineligible.

3. Snapshot mode-aware gating parity
- Apply same mode mapping in snapshot queue inclusion logic.
- Ensure coarse CI status remains stable, while queue-inclusion decisions change by mode.
- Add parity tests to validate queue windows and snapshot agree for equivalent fixtures.

4. Admin/API/docs surface
- Expose mode in admin list/detail and serializer surfaces where ruleset config is shown.
- Document dual-mode semantics in:
  - `docs/design-decisions/011-ci-gating-and-legacy-prs.md`
  - `docs/queueboard_api_contract.md`

5. Rollout + cleanup
- Enable `no_required_failures` on selected rulesets/repositories.
- Monitor queue inclusion deltas and failure-miss signals.
- Remove legacy direct boolean checks once mode migration is complete.

## Validation Plan
- Tests:
  - analyzer queue-window CI mode matrix tests (strict vs no-fail).
  - analyzer snapshot inclusion parity tests against queue-window outcomes.
  - migration/model tests for mode defaults and compatibility.
- Manual checks:
  - For a target repo/ruleset, compare pre/post mode switch queue inclusion for PRs with intentionally skipped jobs.
  - Verify PRs with observed required failures remain excluded in both windows and snapshots.
- Operational checks:
  - Track convergence trends and queue-size changes around rollout.
  - Sample CI ingestion for known failing contexts to validate failure visibility.

## Progress Notes
- 2026-03-02:
  - Dependency `024-per-ruleset-queue-window-build-state` is complete.
  - `023` converted to living-plan format to begin implementation.
  - Current code still uses strict boolean `require_ci_success` behavior.
- 2026-03-03:
  - Chunk 1 started.
  - Added `QueueRuleSet.ci_gating_mode` with strict default and migration `analyzer/0023`.
  - Added transitional resolver helper so `require_ci_success=False` still disables CI gating during migration.
  - Updated `queue_rules`/convergence call sites to use the mode resolver surface.
  - Added initial tests for mode resolution and rules loading surface.
  - Chunk 2 started.
  - Queue windows now evaluate explicit CI states (`pass`/`fail`/`running`/`missing`) and map eligibility by CI mode.
  - Added queue-window tests covering `no_required_failures` behavior for missing/running/failure transitions.
  - Chunk 3 started.
  - Snapshot queue inclusion now uses the same CI mode eligibility mapping as queue windows.
  - Added snapshot tests for `no_required_failures` (missing/running allowed; observed failure blocked).

## Finalization Notes
- After implementation stabilizes, collapse this living plan into a concise final decision record that captures:
  - final ruleset schema/API shape,
  - final queue-window/snapshot invariants,
  - rollout outcomes and any follow-up operational guardrails.

## References
- `docs/design-decisions/004-ci-status-sources.md`
- `docs/design-decisions/011-ci-gating-and-legacy-prs.md`
- `docs/design-decisions/019-ci-by-sha-ledger-and-sha-keyed-ci.md`
- `docs/design-decisions/024-per-ruleset-queue-window-build-state.md`
- `qb_site/analyzer/models/queue_rule.py`
- `qb_site/analyzer/services/queue_windows.py`
- `qb_site/analyzer/services/queueboard_snapshot.py`
- `qb_site/analyzer/services/revisions.py`
