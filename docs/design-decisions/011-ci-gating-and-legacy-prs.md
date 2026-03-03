# CI Gating and Legacy PRs with Missing CI

## Context
- Queue windows for CI-gated rulesets rely on:
  - `syncer.PRTimelineEvent` (labels, draft/ready, reopen/closed, force-push),
  - `analyzer.PRRevision` (head SHA windows across force-pushes),
  - `syncer.CheckRun` / `syncer.StatusContext` (CI snapshots per head SHA and context).
- GitHub expires status/check data after a retention window (around 400 days), so for sufficiently old PRs:
  - CI snapshots may be partially or completely missing.
  - Reviving CI history for older heads via API is not always possible.
- Analyzer’s current queue window logic:
  - For CI-gated rulesets (`require_ci_success=True`), uses `PRRevision` + CI snapshots per head SHA to decide when a PR is "on the queue".
  - For label-only rulesets (`require_ci_success=False`), uses labels/open/draft only.
- CI gating now has explicit mode semantics (see `023`):
  - `all_required_success` (strict, default for CI-enabled rulesets),
  - `no_required_failures` (missing/running required contexts are non-blocking; observed required failures block).
- We want queue windows to be:
  - Correct: never claim CI-gated "on-queue" time when CI is actually unknown.
  - Stable: once persisted, windows should not need to be invalidated when backfills complete.
  - Useful for legacy history: older PRs should still have usable queue windows, even when CI is unrecoverable.

## Decision
- Keep CI-gated rulesets explicit and mode-driven:
  - For rulesets with `require_ci_success=True`, treat missing or unrecoverable CI as "CI unknown" and **do not** mark the PR as on-queue under that ruleset.
  - Persist CI-gated queue windows (`PRQueueWindow`) only when:
    - `timeline_backfill_done` is true for the PR, and
    - At least one `PRRevision` row exists for the PR (head windows are known).
  - CI completeness (per context/head) is assumed to be enforced operationally by Analyzer backfill commands; missing CI simply results in zero windows for that ruleset/PR.
- Introduce explicit label-only "legacy" rulesets:
  - Use `QueueRuleSet.effective_from` / `effective_to` to scope versions:
    - A legacy ruleset (e.g. `version=1`, `require_ci_success=False`) applies to older history where CI may be unrecoverable.
    - A CI-gated ruleset (e.g. `version=2`, `require_ci_success=True`, `required_ci_contexts=[...]`) applies to newer history where CI is reliably available.
  - Analyzer uses effective bounds and per-PR creation time to decide which rulesets to evaluate for each PR.
- Never silently fall back from CI-gated semantics to label-only semantics inside a single ruleset:
  - If `require_ci_success=True` and CI is missing or revisions are absent, CI-gated windows remain empty for that ruleset/PR.
  - Legacy label-only rulesets are separate rows with their own `version` and effective window.
- Mode clarification:
  - For legacy strict CI-gated rulesets, use `ci_gating_mode=all_required_success`.
  - For repositories that intentionally skip required jobs on unaffected SHAs, use `ci_gating_mode=no_required_failures`.

## Consequences
- Pros
  - Clear semantics: each `QueueRuleSet` has a consistent interpretation across all PRs; CI gating is either on or off, never "sometimes".
  - Legacy history is still analyzable:
    - Label-only rulesets yield queue windows and durations for older PRs without CI.
    - CI-gated rulesets cover more recent history where CI and PRRevision are well-populated.
  - Stability: `PRQueueWindow` rows are only persisted when underlying data (timeline + revisions) is stable for the PR; CI backfills can refine windows by recomputation but never flip "unknown CI" to "known" in place.
- Cons
  - For old PRs under CI-gated rulesets, queue windows remain empty even if CI was historically present but later expired.
  - Analysts must explicitly choose which ruleset(s) to use when computing metrics (e.g., "label-only v1" vs "CI-gated v2").

## Operational Notes
- Rulesets
  - `QueueRuleSet` now includes optional `effective_from` / `effective_to` fields:
    - Analyzer uses `effective_from <= at < effective_to` (when set) to pick the appropriate ruleset for a time `at`.
    - `analyzer.tasks.process_pr_task` additionally uses the PR's `gh_created_at` to skip rulesets whose effective window does not include the PR.
  - Recommended pattern per repository:
    - `version=1`: legacy (label-only) ruleset for history before CI is reliable or recoverable (e.g., `effective_to=2024-01-01`).
    - `version=2`: CI-gated ruleset with `require_ci_success=True` and `required_ci_contexts` for history from that date onward (`effective_from=2024-01-01`).
- PRRevision and CI
  - Analyzer only persists CI-gated queue windows when `PRRevision` exists for a PR:
    - For PRs with `timeline_backfill_done == True` but no revisions or CI snapshots, `rebuild_queue_windows_for_ruleset` performs no work and leaves windows empty.
    - CI completeness for revision heads is driven by Analyzer CI backfill tasks (`plan_ci_backfill`, `sync_ci_for_shas`).
  - Instantaneous membership (`is_on_queue_at`) falls back to per-PR CI snapshots when `PRRevision` is absent, but persisted CI-gated windows do not.
- Task orchestration
  - `syncer.sync_pr_task` now triggers `analyzer.process_pr` best-effort:
    - Analyzer rebuilds revisions when timeline backfill is done.
    - Analyzer plans CI-by-SHA backfill for missing revision heads.
    - Analyzer rebuilds `PRQueueWindow` per applicable `QueueRuleSet`, respecting gating rules.
  - Backfill tasks (planned) will drive the same pipeline across older PR cohorts and ruleset versions.

## Alternatives (Optional)
- **Implicit fallback to label-only semantics for old PRs**
  - Pros: CI-gated rulesets would produce windows for legacy PRs even without PRRevision/CI.
  - Cons: semantics depend on PR age; analysts cannot tell whether CI was actually enforced; makes versioned rulesets harder to reason about.
- **Separate "CI availability" flags per PR**
  - Pros: more granular control; some PRs could be flagged as "CI complete" even within older ranges.
  - Cons: extra schema and operational complexity; the effective window mechanism on `QueueRuleSet` already solves the common cases with less machinery.
