# Reviewer Assignment Policy Simulation and Priority Planning (Living Plan)

## Context
- Auto-assignment suggestions live in `qb_site/analyzer/services/reviewer_assignment.py`.
- The current system already has several important pieces in place:
  - queue-derived PR inputs from `QueueSnapshot` / `QueueboardSnapshotBuilder`
  - reviewer eligibility and capacity rules from `core.ReviewerPreference`
  - per-PR reviewer opt-outs from `analyzer.ReviewerOptOut`
  - assignment snapshots and a trace endpoint/payload
- Recent work changed auto-assignment to attempt all queue PRs, not only `QueueStaleUnassigned`.
- Recent work also introduced an initial PR-ranking seam (`PRAssignmentPriority`, `rank_prs_for_assignment(...)`) ahead of assignment.
- We expect to experiment with multiple policy ideas:
  - prioritize older queue PRs
  - prioritize PRs in scarce reviewer areas
  - prioritize specific title patterns such as `feat`
  - compare policy changes under synthetic and historical scenarios
- The current ranking seam is useful, but the current batch algorithm still ranks once at the start of the run instead of re-evaluating priorities after each assignment consumes reviewer capacity.

## Problem Statement
- We need a policy architecture that supports both production assignment and offline experimentation.
- The current code is close to this shape, but not yet explicit enough:
  - policy scoring is modeled as a per-PR sort key, not a round-based decision over current batch state
  - PR ranking is computed once, so reviewer scarcity is only approximated
  - there is no dedicated simulation kernel that can be fed real or synthetic scenarios without Django/task wrappers
- We want to improve policy sophistication without making the assignment loop opaque, fragile, or hard to debug.

## Goals / Non-Goals
- Goals
  - Make PR prioritization an explicit, testable policy layer.
  - Support iterative rescoring after each assignment round.
  - Share the same core engine between production assignment and offline policy simulation.
  - Make policy behavior inspectable with rich trace output.
  - Keep reviewer selection and PR prioritization conceptually separate.
- Non-goals
  - Changing core reviewer eligibility rules in v1 of this refactor.
  - Replacing the current reviewer picker with a global optimizer.
  - Building a full UI for policy simulation in the first pass.
  - Committing to one final priority formula before the engine exists.

## Current State
- `ReviewerAssignmentBuilder.build(...)` currently gathers:
  - reviewers from `ReviewerPreference`
  - existing weighted assignment load from snapshot PRs
  - queue PR numbers from snapshot dashboards
  - opt-outs from `ReviewerOptOut`
- `suggest_reviewers_many(...)` currently:
  - creates an in-memory copy of reviewer load
  - iterates PRs in a chosen order
  - picks one reviewer using existing capacity-aware weighted random choice
  - updates in-memory load after each successful suggestion
- The new ranking seam currently:
  - allows a pluggable `priority_scorer`
  - computes one ordered PR list up front
  - records basic ranking metadata in trace
- Important limitation:
  - the order is not recomputed after reviewer capacity changes within the run

## Decision (Current Plan)
- Refactor reviewer assignment around a pure, round-based simulation kernel.
- Production assignment and offline experiments should both call the same kernel with different input sources and policy hooks.
- Preserve the current reviewer-eligibility and reviewer-pick semantics initially, while moving PR prioritization into a richer round-based policy interface.
- Treat PR prioritization and reviewer choice as separate policy dimensions:
  - PR prioritization decides which PR gets the next chance to consume capacity.
  - reviewer choice decides which eligible reviewer receives that PR.

## Proposed Design

### Core architecture
- Introduce a pure assignment engine in a dedicated nearby module rather than keeping all logic in `reviewer_assignment.py`.
- The engine should operate on plain Python data structures, not ORM objects.
- Django/task/API layers should become thin adapters that:
  - build engine inputs from snapshots/models
  - call the engine
  - persist result payloads / trace
- Preferred module split:
  - `qb_site/analyzer/services/reviewer_assignment_engine.py`
    - pure assignment engine
    - round context/result dataclasses
    - PR priority scoring types
    - reviewer suggestion logic
    - iterative batch loop
  - `qb_site/analyzer/services/reviewer_assignment.py`
    - snapshot/model adapters
    - opt-out lookup
    - reviewer catalog hydration
    - assignment snapshot builder / stored trace shaping
    - area-stats builder
- This boundary is intended to separate:
  - plain-data assignment/simulation behavior
  - Django-facing integration and persistence concerns

### Simulation kernel
- Proposed shape:
  - `SimulationInputs`
    - `prs: dict[int, dict]`
    - `reviewers: list[ReviewerProfile]`
    - `existing_assignments: dict[str, tuple[list[int], float, int]]`
    - `queue_prs: list[int]`
    - `excluded_by_pr: dict[int, set[str]]`
  - `AssignmentRoundContext`
    - `prs`
    - `reviewers`
    - `remaining_prs`
    - `assignment_stats`
    - `excluded_by_pr`
    - `round_index`
  - `PolicyHooks`
    - `priority_scorer(pr_number, ctx) -> PRAssignmentPriority`
    - optional `reviewer_picker(...) -> str | None`
  - `SimulationResult`
    - `assignments: dict[int, str]`
    - `decisions: list[AssignmentDecision]`
    - `final_assignment_stats`

### Round-based algorithm
- The engine should follow this loop:
  1. Initialize mutable in-memory reviewer load from `existing_assignments`.
  2. Initialize `remaining_prs` from `queue_prs`.
  3. Build current round context from remaining PRs and current load.
  4. Score all remaining PRs with `priority_scorer`.
  5. Select the next PR deterministically from the best sort key.
  6. Run reviewer suggestion for that PR using current load and exclusions.
  7. If a reviewer is chosen, update in-memory load.
  8. Record a round decision trace.
  9. Remove the PR from `remaining_prs`.
  10. Repeat until no PRs remain.

### Policy layering
- Keep two separate layers:
  - PR ordering policy:
    - scarcity-aware
    - age-aware
    - title-aware
    - potentially author/label/freshness-aware later
  - reviewer selection policy:
    - initial implementation keeps the current capacity-weighted random choice among eligible reviewers
- This avoids conflating:
  - "which PR should get access to scarce capacity first?"
  - "which reviewer is the best fit once a PR is selected?"

### Initial scorer direction
- After the kernel exists, the first real priority scorer should be lexicographic and deterministic rather than a weighted formula.
- Proposed first sort dimensions:
  - fewer currently-available reviewers first
  - lower total remaining reviewer capacity first
  - older queue age first
  - title bonus such as `feat` before non-`feat`
  - stable tie-breaker by PR number
- This is intentionally a policy direction, not yet a locked final formula.

### Trace and observability
- Trace should become round-aware in the engine, while persisted traces stay compact.
- Keep two trace layers:
  - engine/debug trace
    - round-by-round and potentially verbose
    - optimized for debugging and simulation
  - persisted snapshot trace
    - compact per-PR summary
    - optimized for stable API/storage use
- Engine/debug trace should be able to capture, for each round:
  - round index
  - remaining PRs
  - chosen PR
  - priority details for that PR
  - reviewer suggestion outcome
  - chosen reviewer (if any)
  - updated reviewer load summary
- Persisted snapshot trace should be derived from engine trace/results rather than assembled by separate policy logic.
- The persisted trace does not need to store every intermediate ranking for every round.
- The persisted per-PR summary should be able to answer:
  - when the PR was considered
  - what its priority inputs were at that moment
  - whether it was assigned
  - if not, why not
- We intentionally do not want to pin down the full final persisted trace schema yet.
- Near-term invariants worth pinning down:
  - each PR summary has a round/selection position concept
  - each PR summary includes priority data in some structured form
  - assigned PRs record the selected reviewer
  - unassigned PRs record a stable machine-readable reason
- Near-term flexibility we want to preserve:
  - exact trace field names
  - exact nesting/layout of engine trace
  - exact verbosity of filtered candidate lists and ranking dumps
  - whether engine trace objects are represented as dicts, dataclass dumps, or another structured form

### Simulation support
- The same engine should support:
  - production runs from real queue snapshots
  - historical replay using stored snapshots
  - synthetic scenarios with artificial PR mixes and reviewer pools
- Synthetic scenario generation should be possible with small helper builders that emit:
  - PR dictionaries compatible with assignment logic
  - reviewer profiles
  - starting assignment/load state
  - opt-out maps

## Invariants / Subtleties
- Capacity-consumption invariant
  - PR priority should be evaluated against current in-memory reviewer load, not just the initial load at the start of the batch.
- Separation-of-concerns invariant
  - PR prioritization must not smuggle reviewer-overload pressure into reviewer selection in a way that bypasses existing capacity/eligibility rules.
- Determinism invariant
  - PR selection order for a given round state should be deterministic.
  - Randomness, if retained, should remain confined to reviewer choice among eligible candidates.
- Purity/portability invariant
  - The core simulation engine should not depend on Django ORM queries during the loop.
- Traceability invariant
  - Policy decisions should be explainable from emitted trace data.
- Trace layering invariant
  - Stored snapshot trace should be a compact projection of engine output, not a separately-implemented parallel explanation path.
- Backward-compatibility concern
  - Existing stored payloads and API consumers should continue to receive assignment snapshots; any trace enrichment should be additive where possible.

## Tension Points / Risks
- Engine purity vs code duplication
  - Pulling logic out of Django wrappers can duplicate small adapter code.
  - This is acceptable if it keeps the core engine deterministic and easy to simulate.
- Trace detail vs payload size
  - Round-by-round traces can become large on busy queues.
  - Mitigation: keep the engine capable of verbose tracing, but store a compact production trace by default.
- Policy flexibility vs readability
  - A fully generic policy API can become abstract and under-explained.
  - Mitigation: keep the first scorer lexicographic and record named dimensions in trace details.
- Historical replay fidelity
  - Stored snapshots may not capture every future policy signal we eventually want.
  - Mitigation: design synthetic-scenario tooling alongside historical replay, not instead of it.
- Reviewer picker experiments
  - Once a simulation kernel exists, it becomes tempting to also vary reviewer selection aggressively.
  - Mitigation: defer reviewer-picker policy experiments until PR-priority behavior is stable.

## Implementation Plan (Chunks)
1. Extract a pure assignment simulation kernel.
   - Introduce input/context/result dataclasses.
   - Move the batch loop into a pure function that operates on plain data.
   - Keep current behavior the same as much as possible.

2. Convert ranking from one-shot to round-based.
   - Replace "rank once then iterate" with iterative rescoring over remaining PRs.
   - Keep the default scorer behavior neutral/deterministic.

3. Adapt production builder and trace paths.
   - Make `ReviewerAssignmentBuilder.build(...)` call the simulation kernel.
   - Make `build_reviewer_assignment_trace(...)` derive output from kernel trace/results.
   - Preserve current stored payload shape for `automatic_assignments`.
   - Keep persisted trace compact even if engine trace is richer.

4. Expand tests around the pure engine.
   - Add service-level tests for:
     - stable default behavior
     - iterative rescoring after capacity changes
     - opt-out handling in round-based ordering
     - deterministic tie-breaking
   - Keep trace-shape assertions intentionally light:
     - assert key semantics/invariants, not exact full trace blobs

5. Add the first real PR-priority scorer.
   - Implement scarcity/age/title lexicographic scoring.
   - Emit named policy dimensions in trace details.
   - Validate behavior on representative queue mixes.

6. Add offline simulation helpers.
   - Add synthetic scenario builders and/or a management command/script entrypoint.
   - Support comparison between baseline and candidate policies on the same inputs.

7. Add experiment metrics/reporting.
   - Compute outputs such as:
     - assignment count
     - average / tail queue age of assigned PRs
     - assignment rate by area label
     - load distribution across reviewers
     - unassigned scarce-PR counts

## Validation Plan
- Unit/service tests
  - `rank_prs_for_assignment` or its successor behaves deterministically.
  - Round-based rescoring changes PR order when reviewer capacity changes.
  - Production wrapper results match engine results on the same inputs.
  - Opt-outs and conflict filters remain unchanged in meaning.
- Scenario tests
  - artificial scarce-area scenarios
  - mixed old/new PR scenarios
  - title-bonus scenarios
  - reviewer-away / low-capacity scenarios
- Regression checks
  - assignment snapshot payload still contains stable `automatic_assignments`
  - trace output remains parseable and useful
- Local checks
  - `uv run python -m py_compile ...`
  - targeted Django tests when Postgres is available
  - broader repo checks via `bash scripts/repo_check_compose.sh` when environment permits

## Rollout Plan
- Phase 0
  - land pure engine and round-based rescoring with neutral default scorer
  - keep policy behavior as close as possible to current output
- Phase 1
  - introduce first real scorer behind a narrow code seam
  - compare outputs against current baseline on representative snapshots
- Phase 2
  - add offline simulation tooling for synthetic and historical experiments
  - tune priority dimensions based on measured behavior
- Phase 3
  - clean up living plan into a concise ADR once policy and engine shape stabilize

## Open Questions
- Should the core engine remain in `analyzer/services/reviewer_assignment.py`, or should it move into a dedicated `assignment_simulation.py` module once it grows?
- What subset of round trace should be stored in `ReviewerAssignmentSnapshot` versus only used in debugging/tests?
- Should the first scorer use exact queue age seconds, or bucketed age tiers for easier explainability?
- How much policy configurability do we want in code vs settings/admin data?
- Do we want a dedicated management command for offline experiments, or is a Python module/script sufficient at first?

## Progress Notes
- 2026-03-15:
  - Drafted this living plan after changing assignment inputs to all queue PRs and adding an initial PR-ranking seam.
  - Current code has `PRAssignmentPriority` and `rank_prs_for_assignment(...)`, but still ranks once per batch.
  - No pure simulation kernel exists yet.
  - Follow-up plan refinement:
    - prefer splitting pure engine logic into a dedicated `reviewer_assignment_engine.py`
    - keep trace requirements semantic rather than over-constraining exact shape during early refactors
- 2026-03-15:
  - Chunk 1 started:
    - extracted pure assignment primitives and batch execution into `qb_site/analyzer/services/reviewer_assignment_engine.py`
    - rewired `reviewer_assignment.py` to act as the snapshot/model integration layer over the engine
    - preserved current batch semantics, including one-shot ranking, so iterative rescoring remains future work in chunk 2
- 2026-03-15:
  - Chunk 2 started:
    - updated the engine loop to rescore remaining PRs after each assignment round using current in-memory reviewer load
    - kept the persisted trace compact by recording selection round and per-round priority data only for the chosen PR
    - added service-level coverage that distinguishes iterative rescoring from the old one-shot ranking behavior

## References
- `docs/design-decisions/README.md`
- `docs/design-decisions/020-reviewer-opt-outs-and-timeline-assignments.md`
- `docs/design-decisions/033-backend-driven-queue-evaluation-and-on-the-queue.md`
- `qb_site/analyzer/services/reviewer_assignment.py`
- `qb_site/analyzer/tasks/reviewer_assignment.py`
- `qb_site/api/views/reviewer_assignment.py`
- `qb_site/analyzer/tests/services/test_reviewer_assignment.py`
