# Reviewer Assignment Engine and Priority Policy

## Context
- Auto-assignment suggestions are built from queue snapshots in `qb_site/analyzer/services/reviewer_assignment.py`.
- The system already depended on several long-lived constraints:
  - reviewer eligibility and capacity come from `core.ReviewerPreference`
  - per-PR exclusions come from `analyzer.ReviewerOptOut`
  - suggestions are stored as `ReviewerAssignmentSnapshot` payloads and exposed via the existing API/task paths
- We needed to evolve assignment ordering to support richer policy behavior without entangling it with Django/model concerns.
- We also needed scarcity-aware prioritization to be correct under capacity consumption during a batch, which requires rescoring remaining PRs after each assignment round.

## Decision
- Split reviewer assignment into:
  - a pure engine module: `qb_site/analyzer/services/reviewer_assignment_engine.py`
  - a Django/snapshot integration module: `qb_site/analyzer/services/reviewer_assignment.py`
- The engine is the source of truth for:
  - reviewer suggestion logic
  - PR ranking logic
  - in-memory assignment state updates across a batch
- The integration layer is responsible for:
  - hydrating reviewer profiles from `ReviewerPreference`
  - reading opt-outs from `ReviewerOptOut`
  - deriving initial assignment load from snapshot payloads
  - filtering assignment candidates to queue PRs that are not already assigned to an active reviewer
  - building/storing assignment snapshots and compact trace payloads
- The engine now rescoring remaining PRs after each round is the canonical batch behavior.
- The default production priority policy is deterministic and lexicographic:
  - queue PRs already assigned to an active reviewer are excluded from assignment candidates
  - PRs without a topic label are not auto-assigned and should be handled earlier in triage
  - assignable PRs before currently-unassignable PRs
  - fewer available reviewers first
  - lower total remaining reviewer capacity first
  - older queue age first, using `total_queue_time.value_td` when valid
  - `feat` title bonus as a weak final tiebreak before PR number
- Trace is layered:
  - engine-level behavior may be richer internally
  - persisted snapshot trace remains compact and per-PR
  - persisted trace records key semantics rather than a full round dump

## Consequences
- Assignment policy is easier to evolve without mixing ORM/persistence concerns into the batch loop.
- Scarcity-aware ordering is now materially more correct because it reacts to reviewer capacity consumed earlier in the same run.
- The compact persisted trace remains stable enough for operational inspection while leaving room to evolve the engine’s internal trace shape.
- The default live behavior changed:
  - queue PRs are considered only if they are not already assigned to an active reviewer
  - queue PRs without topic labels are intentionally left unassigned
  - ordering now reflects the new default priority policy
  - production builds use the extracted engine path with no feature flag
- Future simulation and policy-comparison tooling is easier to add because the engine operates on plain data structures.

## Operational Notes
- Key modules:
  - engine: `qb_site/analyzer/services/reviewer_assignment_engine.py`
  - integration/builders: `qb_site/analyzer/services/reviewer_assignment.py`
  - service exports: `qb_site/analyzer/services/__init__.py`
- Current persisted trace expectations:
  - each PR summary carries a round/selection position concept
  - each PR summary includes structured priority data
  - assigned PRs record the selected reviewer
  - unassigned PRs record a machine-readable reason
- Testing status:
  - service-level coverage exists for ranking seams, iterative rescoring, assignability, scarcity, queue age, and title-bonus behavior
  - full Django test execution still depends on PostgreSQL availability in the execution environment
- Deferred follow-up work:
  - offline simulation helpers and experiment/reporting tooling
  - additional policy dimensions or configurability beyond the current default scorer
  - any future expansion of stored trace shape beyond the compact per-PR form

## Alternatives
- Keep all logic in `reviewer_assignment.py`:
  - rejected because engine behavior, snapshot adaptation, and persistence concerns were starting to drift together
- Rank the batch once at the start:
  - rejected because scarcity-aware ordering becomes stale as reviewer capacity is consumed
- Encode scarcity pressure directly in reviewer selection rather than PR ordering:
  - rejected because it conflates two different decisions and makes the batch harder to explain and test
