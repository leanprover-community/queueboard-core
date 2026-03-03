# CI Gating Mode: No Required Context Failures

## Context
- Strict CI gating previously used a boolean (`QueueRuleSet.require_ci_success`) and required every required context to be observed and passing.
- Some required jobs are intentionally not triggered on unaffected SHAs; strict semantics could keep PRs permanently off-queue despite expected CI behavior.
- Queue-window and snapshot logic both depend on ruleset CI semantics, so they must stay aligned.
- `024` introduced per-ruleset queue-window build-state, enabling targeted recomputation when ruleset semantics change.

## Decision
- Introduce explicit CI gating modes on `QueueRuleSet` via `ci_gating_mode`:
  - `all_required_success` (default strict mode),
  - `no_required_failures`.
- Effective mode is derived through `effective_ci_gating_mode()`:
  - `require_ci_success=False` => CI gating disabled (`None`),
  - otherwise mode is `ci_gating_mode` with strict fallback.
- Mode semantics:
  - `all_required_success`: queue-eligible only when required contexts are observed and passing.
  - `no_required_failures`: queue-eligible unless required contexts are observed failing.
  - In no-fail mode, missing/unobserved and running/pending required contexts are non-blocking.
- Keep ruleset behavior explicit per row; no implicit time-based fallback.

## Consequences
- Pros:
  - Supports repositories that intentionally skip some required jobs.
  - Avoids false "perma-off-queue" outcomes from expected non-triggered contexts.
  - Preserves strict mode as default and keeps behavior auditable at ruleset level.
- Trade-offs:
  - CI-gated semantics now have two valid interpretations; operator/docs clarity is required.
  - Correctness emphasis under no-fail mode shifts toward reliable ingestion of failures.

## Operational Notes
- Implemented schema/model:
  - Added `QueueRuleSet.ci_gating_mode` (migration `analyzer/0023_queueruleset_ci_gating_mode.py`).
  - Added compatibility resolver + `effective_ci_gating_mode()`.
- Implemented analyzer behavior:
  - Queue windows evaluate explicit CI states (`pass`/`fail`/`running`/`missing`) and map eligibility by effective mode.
  - Snapshot queue inclusion uses the same mode mapping as queue windows.
  - Snapshot metadata exposes ruleset CI config: `ci_gating_mode`, `require_ci_success` (effective), `required_ci_contexts`, `rule_set_version`.
  - Mode-sensitive convergence/filtering paths use effective mode helpers.
- Rollout guidance:
  - Existing strict rulesets continue unchanged by default (`all_required_success`).
  - Enable `no_required_failures` by creating/updating specific rulesets where skipped jobs are expected.
  - Use per-ruleset window freshness (`024`) to limit rebuild scope.

## Alternatives (Optional)
- Keep strict-only semantics and require explicit pass emission for skipped jobs.
  - Rejected: often infeasible when jobs are not triggered.
- Infer pass after prolonged missing context.
  - Rejected: hidden heuristic, hard to audit.
- Remove required-context gating and rely only on aggregate CI.
  - Rejected: loses essential-context control.

## References
- `docs/design-decisions/004-ci-status-sources.md`
- `docs/design-decisions/011-ci-gating-and-legacy-prs.md`
- `docs/design-decisions/019-ci-by-sha-ledger-and-sha-keyed-ci.md`
- `docs/design-decisions/024-per-ruleset-queue-window-build-state.md`
- `qb_site/analyzer/models/queue_rule.py`
- `qb_site/analyzer/services/queue_rules.py`
- `qb_site/analyzer/services/queue_windows.py`
- `qb_site/analyzer/services/queueboard_snapshot.py`
