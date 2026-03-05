# Backend-Driven Queue Evaluation and Adaptive On-the-Queue (Living Plan)

## Context
- We now have ruleset-driven queue semantics in Analyzer (`QueueRuleSet`, CI gating modes, per-ruleset queue windows/snapshots), including `no_required_failures` from `023`.
- The `on_the_queue` page in legacy renderer code (`src/queueboard/dashboard.py`) still recomputes queue-check booleans in frontend logic using mostly hardcoded predicates.
- This split creates semantic drift:
  - Backend computes canonical queue membership (`snapshot.lists.dashboards.Queue`) from rulesets.
  - Frontend computes "On the review queue?" from a separate checklist that is not a full `QueueRuleSet` evaluation.
- Recent `no_required_failures` behavior exposed this drift: backend queue eligibility and frontend "CI status? / overall" could disagree.
- Existing docs already frame key constraints:
  - `010`: queue windows are ruleset-driven and canonical in Analyzer.
  - `011`: no implicit fallback from CI-gated to label-only within a ruleset.
  - `023`: explicit CI gating modes with non-equivalent semantics.
  - `024`: per-ruleset freshness/build-state reinforces ruleset-specific truth.

## Goals / Non-Goals
- Goals
  - Make backend snapshot payload the source of truth for queue eligibility explanations shown in `on_the_queue`.
  - Eliminate (or minimize) frontend recomputation of queue predicates.
  - Expose explicit per-PR reasons for queue-in/out state so UI can explain "why" directly from backend decisions.
  - Keep dashboard useful and readable across different rulesets (including non-legacy/custom label rules).
- Non-goals
  - Full redesign of all legacy dashboards in one pass.
  - Replacing legacy HTML renderer immediately; this plan targets correctness-first migration while preserving existing pages.
  - Changing queue semantics themselves (this plan is representation/explanation alignment, not semantics changes).

## Problem Framing
- Current `on_the_queue` columns are legacy, mostly mathlib-specific checks:
  - fixed labels (`blocked-by-*`, `merge-conflict`, `awaiting-*`, `WIP/help-wanted/please-adopt`),
  - hardcoded base branch string (`master`),
  - CI column semantics historically strict-pass-centric.
- `QueueRuleSet` is more general:
  - arbitrary required/forbidden labels,
  - explicit CI modes and required contexts,
  - repo default-branch awareness,
  - append-only versioned rulesets and active/effective bounds.
- Therefore a one-to-one mapping from legacy columns to canonical rules does not always exist.
- The table must evolve from "legacy checklist" to "ruleset evaluation report".

## Proposed Design
- Add backend-emitted queue evaluation data per PR in snapshot payload.
- Frontend consumes this data directly (no local eligibility logic beyond presentation).

### Snapshot schema extension (proposed)
- Add per-PR field `queue_evaluation` in `snapshot.prs[pr_number]`:
  - `is_on_queue_now: bool`
  - `is_queue_candidate_now: bool`
  - `blocking_reasons: [reason_code]`
  - `non_blocking_notes: [note_code]`
  - `predicates: { ... }` where each predicate has:
    - `status: pass|fail|unknown|not_applicable`
    - `detail: str` (short backend-provided explanation)
    - optional machine fields (e.g. matched labels/contexts)
- Add `queue_rules_applied` in `snapshot.meta`:
  - resolved/effective rule parameters used for evaluation (`require_open`, `require_not_draft`, `required_labels`, `forbidden_labels`, `ci_gating_mode`, `required_ci_contexts`, `default_branch`).

### Predicate model (proposed initial set)
- `base_branch_allowed`
- `open_required`
- `not_draft_required`
- `required_labels_present`
- `forbidden_labels_absent`
- `ci_eligible`
- Optional informational predicates (non-blocking):
  - `has_merge_conflict_label` (if retained as policy signal or UX hint)
  - `legacy_topic_label_present` (mathlib-specific advisory; never queue-blocking)

### Backend ownership
- Analyzer snapshot builder computes all predicate statuses from the same rule-evaluation path used for queue dashboards.
- `on_the_queue` page should derive "On the review queue?" from `queue_evaluation.is_queue_candidate_now` (or directly from backend `is_on_queue_now`, based on final naming choice), not from frontend recomputation.

### Frontend/table adaptation
- Keep first iteration close to current UX but backend-driven:
  - Preserve existing columns where they map cleanly to predicates.
  - Replace ambiguous legacy columns with ruleset-aware columns.
- Move explanation text (`EXPLANATION_ON_THE_QUEUE_PAGE`) from hardcoded mathlib checklist to dynamic, ruleset-derived summary built from `meta.queue_rules_applied`.
- Tooltips should use backend `detail` strings + structured reason codes.

## Subtleties / Invariants
- Canonical invariant
  - Queue eligibility explanation must be computed from the same backend logic as queue inclusion lists.
- Snapshot-time invariant
  - `queue_evaluation` reflects the snapshot timestamp (`meta.generated_at`) and may differ from live repo state after that instant.
- Ruleset invariant
  - Explanations are only valid for the snapshot rule set (`meta.rule_set_id` / version).
- CI semantics invariant
  - `ci_eligible` must remain mode-aware (`all_required_success` vs `no_required_failures`) and must not be reinterpreted in frontend.
- No implicit fallback invariant
  - Preserve `011` behavior: if CI is unknown in a CI-gated ruleset, report as such; do not silently reinterpret with label-only semantics.

## Tension Points / Risks
- Legacy familiarity vs correctness
  - Existing users recognize legacy columns; a direct switch to abstract predicates may reduce readability.
  - Mitigation: staged UI transition with stable labels + richer tooltips + short migration note.
- One-column-per-check vs dynamic predicates
  - Fixed columns are easier to scan but can misrepresent custom rulesets.
  - Dynamic columns are accurate but can destabilize table UX/search/sort.
  - Proposed compromise: fixed "core" columns + expandable "rule checks" detail cell.
- Candidate vs actual queue inclusion
  - Legacy page asks "On the review queue?" while backend has multiple derived lists and special routing (e.g. NeedsMerge).
  - We must explicitly choose whether the displayed boolean means:
    - strict eligibility under queue rules, or
    - literal membership in `lists.dashboards.Queue`.
- Payload size growth
  - Per-PR predicate objects can significantly increase snapshot payload size.
  - Mitigation: compact reason codes + concise details + optional verbosity mode later.
- API compatibility
  - Some consumers may assume current snapshot shape.
  - Mitigation: additive fields only in first phase; keep existing fields until migration complete.
- Mathlib-specific advisory checks
  - "Missing topic label" is not queue-gating but remains useful.
  - Keep as explicit advisory in backend (or a frontend-only advisory section clearly separated from queue predicates).

## Implementation Plan (Chunks)
1. Define canonical queue evaluation schema
- Add typed structures in Analyzer for predicate/reason emission.
- Choose reason code vocabulary and status enum.
- Document schema update in `docs/queueboard_api_contract.md`.

2. Implement backend emission in snapshot builder
- Compute `queue_evaluation` per PR in `QueueboardSnapshotBuilder` using existing ruleset logic paths.
- Add `queue_rules_applied` in `meta`.
- Ensure `is_queue_candidate_now` (or selected canonical boolean) is derived from same code path as queue list inclusion.

3. Backend tests
- Extend `qb_site/analyzer/tests/test_queueboard_snapshot.py`:
  - strict mode and `no_required_failures` cases,
  - missing/running/fail CI transitions,
  - required/forbidden label cases,
  - consistency assertions between emitted booleans and queue list placement.

4. Frontend consume-only migration for on_the_queue
- Update `src/queueboard/snapshot.py` loader to parse new fields.
- Update `src/queueboard/dashboard.py` `write_on_the_queue_page` to use backend `queue_evaluation` fields, removing local rule recomputation.
- Adapt explanatory text to ruleset metadata (dynamic checklist summary).

5. UX adaptation pass
- Decide final table shape:
  - Option A: mostly existing columns mapped to backend predicates.
  - Option B: new "Rule Checks" + "Blocking reasons" columns.
- Keep/search/sort behavior usable with minimal disruption.

6. Cleanup and deprecation
- Remove dead frontend logic for queue recomputation.
- Keep backward-compat fallback for older snapshots for a short transition window.
- Follow up with ADR cleanup once rollout is complete.

## Validation Plan
- Unit tests
  - Analyzer snapshot tests for queue evaluation schema + semantics.
  - Snapshot loader tests for backward compatibility.
- Integration checks
  - Generate dashboards from API snapshots for at least two rule sets (`all_required_success`, `no_required_failures`) and verify row-level consistency.
- Consistency assertions
  - For each PR in snapshot at render time:
    - `queue_evaluation.is_queue_candidate_now == (pr in dashboards.Queue or explicit documented exception)`.
- Manual checks
  - Compare exported `on_the_queue` table rows on representative cohorts:
    - legacy old PRs with missing CI,
    - recent PRs with pending CI,
    - PRs blocked by labels,
    - custom-ruleset label constraints.

## Open Questions
- Final canonical boolean for UI:
  - `is_queue_candidate_now` vs `is_on_queue_now` vs both?
- Relationship with `NeedsMerge` routing:
  - Should merge-conflict PRs be shown as "eligible except merge conflict" or strictly "not on queue"?
- Predicate visibility model:
  - Fixed column subset + details popup, or dynamic columns based on active ruleset?
- Advisory checks ownership:
  - Should non-gating checks like topic-label hints be backend-emitted advisory predicates?
- Contract versioning:
  - Is this additive under `v1-draft`, or do we bump schema version string once frontend depends on new fields?

## Progress Notes
- 2026-03-04: Initial living plan drafted. Prompted by observed drift between `no_required_failures` backend semantics and `on_the_queue` frontend checklist logic.

## Finalization Notes
- After rollout, convert this living plan into a concise ADR that states:
  - backend queue evaluation is canonical,
  - frontend consumes explanations without recomputation,
  - the final on_the_queue column model and semantics.
