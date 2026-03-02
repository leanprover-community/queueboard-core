# CI Gating Mode: No Required Context Failures

## Implementation Status (as of 2026-03-02)
- Not yet implemented.
- Current gating model remains boolean `require_ci_success` on `QueueRuleSet`
  (`qb_site/analyzer/models/queue_rule.py`), with strict required-context semantics
  in analyzer queue-window/snapshot logic.
- `019` Part 1 (CI-by-SHA ledger/backoff) is already implemented and can be reused.
- `019` Part 2 (SHA-keyed CI tables) is not a prerequisite for initial rollout.
- `024` (per-ruleset queue-window build state) should be implemented first to avoid
  unnecessary full-ruleset recomputation as ruleset semantics evolve.

## Context
- Today, CI-gated queue rules use a strict interpretation: required CI contexts must be observed and successful before a PR is considered on-queue.
- That strict interpretation is intentional and conservative, and is documented in:
  - `docs/design-decisions/011-ci-gating-and-legacy-prs.md`
  - `docs/design-decisions/004-ci-status-sources.md`
- Current implementation touchpoints:
  - Queue window CI gating in `qb_site/analyzer/services/queue_windows.py`
  - Snapshot CI rollup and queue inclusion in `qb_site/analyzer/services/queueboard_snapshot.py`
  - Ruleset model in `qb_site/analyzer/models/queue_rule.py`
  - CI backfill candidate selection in `qb_site/analyzer/services/revisions.py` (`next_revision_backfill_shas`)
- We now have a real operational need where some required jobs are intentionally not triggered for certain SHAs (for efficiency), for example when a workflow determines that the change cannot affect that job's scope.
- In those cases, strict "must observe success for every required context" semantics can incorrectly keep PRs off-queue forever, even though this is an expected and safe CI behavior.

## Decision
- Introduce explicit CI gating modes on `QueueRuleSet` so behavior is not implicit:
  - `all_required_success` (existing behavior, default)
  - `no_required_failures` (new behavior)
- Semantics by mode:
  - `all_required_success`
    - Required context must be observed and successful for the relevant head SHA/time.
    - Missing/unobserved/running contexts are not sufficient.
  - `no_required_failures`
    - A PR is CI-eligible if none of the required contexts are observed in a failing state for the relevant head SHA/time.
    - Missing/unobserved/not-triggered contexts do not block queue eligibility.
    - Observed running/pending contexts do not block queue eligibility unless they later report failure.
- Keep mode selection explicit per ruleset row (no silent fallback based on PR age or data availability).
- Continue to scope rule behavior via `effective_from`/`effective_to` and versioning.

## Clarification: Backfill Convergence Concern
- Earlier concern: planner/backfill convergence is currently keyed to "has some CI for SHA" or "terminal SHA fetch outcome", not "has observed every required context for SHA".
- Concretely, `next_revision_backfill_shas` in `qb_site/analyzer/services/revisions.py` plans more CI fetches mainly when:
  - no CheckRun/StatusContext exists for a SHA, or
  - only pending status contexts exist, or
  - check runs are still queued/in-progress.
- It does not currently inspect the active ruleset's required context list and ask "have all required contexts been observed?"
- Why that mattered under strict mode:
  - Under `all_required_success`, a missing required context blocks queue entry.
  - If backfill stops after seeing some CI for a SHA, but misses one required context forever, strict mode can remain blocked due to incomplete context coverage.
- Why this is less problematic under the new mode:
  - Under `no_required_failures`, missing or never-triggered required contexts are acceptable.
  - Therefore, planner convergence that does not guarantee complete required-context coverage is compatible with semantics, as long as observed failures are ingested reliably.
- New operational emphasis under `no_required_failures`:
  - Prioritize reliable ingestion of failures for observed required contexts.
  - Complete observation of every required context is no longer required for eligibility.

## Consequences
- Pros
  - Matches real CI behavior where jobs may be intentionally skipped/not-triggered for unaffected SHAs.
  - Prevents "perma-off-queue" outcomes caused by expected non-execution of contexts.
  - Keeps semantics explicit and auditable through a ruleset field, not hidden heuristics.
- Cons
  - Less conservative than strict mode: unknown/missing contexts no longer imply "off-queue."
  - Requires careful messaging in docs and admin because "CI-gated" now has two meanings.
  - Queue windows and snapshot CI status logic need coordinated updates to avoid divergence.

## Operational Notes
- Schema/model
  - Add a CI mode field on `QueueRuleSet` (string enum), defaulting to strict mode.
  - Keep `require_ci_success` for backward compatibility during rollout, then simplify once callers are migrated.
- Queue windows (`qb_site/analyzer/services/queue_windows.py`)
  - Replace binary CI helper with a richer CI state evaluation to distinguish:
    - fail
    - running/pending
    - missing/unobserved
    - pass
  - Apply mode-specific gating:
    - strict: only pass is eligible
    - no-fail: pass/running/missing are eligible; fail is ineligible
  - Ensure boundary generation still captures transitions that can flip membership.
- Snapshot (`qb_site/analyzer/services/queueboard_snapshot.py`)
  - Use the same mode semantics for queue inclusion as queue windows.
  - Keep coarse CI status (`pass`, `fail`, `running`, `missing`, `fail-inessential`) but derive queue eligibility from mode.
  - Revisit `_queue_data_status` so "no windows" is not auto-labeled `missing` in cases where no-fail semantics legitimately yield sparse windows.
- CI ingestion/backfill
  - No mandatory redesign required for first rollout of no-fail mode.
  - Continue CI-by-SHA/backoff strategy from `docs/design-decisions/019-ci-by-sha-ledger-and-sha-keyed-ci.md`.
  - Monitor false negatives for failures (the critical risk under no-fail mode) rather than completeness of non-failing contexts.
- Tests
  - Add queue-window tests for no-fail mode:
    - missing required context -> eligible
    - pending/running required context -> eligible
    - observed required failure -> ineligible
  - Add snapshot tests asserting mode-consistent queue inclusion for the same CI states.
  - Keep strict-mode tests unchanged to preserve backwards-compatible behavior.
- Documentation
  - Update `docs/design-decisions/011-ci-gating-and-legacy-prs.md` to reference this decision and clarify that strict semantics remain available.
  - Update `docs/queueboard_api_contract.md` to document that queue eligibility depends on ruleset CI mode.

## Rollout Plan
- Phase 1: add mode field with strict default; keep all behavior effectively unchanged.
- Phase 2: implement dual-mode evaluators in queue windows and snapshot, plus tests.
- Phase 3: enable no-fail mode for selected repositories/rulesets where skipped/not-triggered jobs are expected.
- Phase 4: evaluate metrics and convergence dashboards; expand usage if results are stable.

## Alternatives (Optional)
- Keep strict mode only and encode every intentional skip as explicit pass
  - Rejected: often impractical when jobs are not triggered and no explicit success is emitted.
- Remove required context list entirely and gate on overall rollup only
  - Rejected: loses ability to constrain queue semantics to essential contexts.
- Silent heuristic ("if context missing for long enough, treat as pass")
  - Rejected: hidden behavior, hard to reason about, and difficult to audit.
