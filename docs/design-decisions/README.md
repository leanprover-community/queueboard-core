# Design Decisions

This directory captures concise, versioned decisions that affect architecture, runtime behavior, or operational posture. Treat each file as a durable record that future contributors can scan quickly.

## When to Write One
- You’re choosing between credible alternatives (e.g., libraries, deployment patterns, schemas).
- The choice impacts more than one module or has operational consequences.
- You want a stable reference that outlives PR discussions and chat threads.

## File Naming
- Use a zero‑padded numeric prefix and a short slug: `NNN-short-title.md`.
- Increment the prefix for new decisions (see existing `000-`, `001-`, `002-`).

## Structure
- Context: the problem and constraints in bullets.
- Decision: the chosen option, stated clearly and concisely.
- Consequences: trade‑offs; what gets easier or harder.
- Operational Notes: rollout steps, flags, migrations, follow‑ups.
- (Optional) Alternatives: briefly note discarded options and why.

## Style
- Prefer bullets and short sentences over long prose.
- One decision per file; link related decisions rather than combining.
- Reference concrete files/paths and commands where useful.
- Link to PRs, issues, or external docs for deeper context.

## Location & Scope
- Keep decisions here under `docs/design-decisions/`.
- Component‑specific choices can still live here; mention the scope in “Context”.

## Example Skeleton
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
