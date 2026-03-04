# Design Decisions

This directory captures versioned architecture/design records that affect runtime behavior, operations, or long-lived implementation direction. Files here can be either concise final decisions or implementation-driven living plans that are refined as work progresses.

## When to Write One
- You’re choosing between credible alternatives (e.g., libraries, deployment patterns, schemas).
- The choice impacts more than one module or has operational consequences.
- You want a stable reference that outlives PR discussions and chat threads.

## File Naming
- Use a zero‑padded numeric prefix and a short slug: `NNN-short-title.md`.
- Increment the prefix for new decisions (see existing `000-`, `001-`, `002-`).

## Document Types
- Final decision record (concise ADR style):
  - Used when scope is settled and implementation is complete (or nearly complete).
  - Preferred shape: `Context`, `Decision`, `Consequences`, `Operational Notes`, optional `Alternatives`.
- Living implementation plan (work-in-progress design doc):
  - Used for larger features where implementation proceeds in chunks and details evolve.
  - Expected lifecycle:
    1. Start with a detailed plan emphasizing correctness/completeness over brevity.
    2. Keep the doc updated while implementing testable chunks; capture plan changes and discovered nuances.
    3. After implementation, clean up into a coherent final architecture/design record.
  - This format is explicitly allowed in this directory and should be preferred for multi-step work with non-trivial subtleties.

## Structure
- Final decision record:
  - Context: the problem and constraints in bullets.
  - Decision: the chosen option, stated clearly and concisely.
  - Consequences: trade‑offs; what gets easier or harder.
  - Operational Notes: rollout steps, flags, migrations, follow‑ups.
  - (Optional) Alternatives: briefly note discarded options and why.
- Living implementation plan:
  - Problem framing and goals.
  - Proposed architecture/plan (with subtleties and invariants).
  - Chunked implementation plan and test strategy.
  - Progress notes / deltas discovered during implementation.
  - Final cleanup section (or follow-up pass) to converge on durable architecture docs.

## Converting a Living Plan to a Final Decision
- Convert once implementation is complete (or close enough that major architecture choices are settled).
- Keep the same file and numeric prefix unless there is a strong reason to split scope.
- Rewrite into concise ADR shape:
  - `Context` (what problem/constraints mattered),
  - `Decision` (what is now true in architecture/runtime),
  - `Consequences` (trade-offs),
  - `Operational Notes` (current status + remaining follow-ups),
  - optional `Alternatives`.
- Remove or collapse:
  - chunk-by-chunk rollout instructions,
  - stale feature-flag sequencing,
  - long progress logs that are no longer needed for current operation.
- Preserve:
  - final invariants,
  - important migration outcomes,
  - explicitly deferred follow-up work.

## Style
- Prefer bullets and short sentences over long prose.
- One main theme per file; link related decisions/plans rather than combining unrelated topics.
- Reference concrete files/paths and commands where useful.
- Link to PRs, issues, or external docs for deeper context.
- For living plans, prioritize technical correctness and explicit invariants; brevity is secondary until final cleanup.

## Location & Scope
- Keep decisions here under `docs/design-decisions/`.
- Component‑specific choices can still live here; mention the scope in “Context”.

## Example Skeleton (Final Decision Record)
```
# Title

## Context
- ...

## Decision
- ...

## Consequences
- ...

## Operational Notes
- ...

## Alternatives (Optional)
- ...
```

## Example Skeleton (Living Implementation Plan)
```
# Title

## Context
- ...

## Goals / Non-Goals
- ...

## Proposed Design
- ...

## Subtleties / Invariants
- ...

## Implementation Plan (Chunks)
1. Chunk 1 ...
2. Chunk 2 ...

## Validation Plan
- tests:
- manual checks:

## Progress Notes
- YYYY-MM-DD: ...

## Finalization Notes
- Follow-up cleanup to produce durable final architecture summary.
```
