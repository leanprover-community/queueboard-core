# Preferred Label Storage for Reviewer Preferences

## Context
- Reviewer suggestions in v1 use GitHub labels as “topics” for matching PRs to reviewers.
- We need to store each reviewer’s label preferences alongside capacity and rotation settings.
- We considered three storage options:
  - JSON list of label names on `core.ReviewerPreference` (no FK).
  - M2M to `syncer.LabelDef` (FK to ingested label definitions).
  - Normalized core table with `label_name` (no FK), repo‑scoped.

## Decision
- Store preferred labels as a JSON list of label names on `core.ReviewerPreference`.
- Keep core decoupled from syncer in v1; avoid a `core → syncer` dependency.
- Add a validator (management command or background check) to warn when preferred label names do not exist in the repository’s label catalog once syncer is active.

## Consequences
- Pros
  - Simple import from `reviewer-topics.json`; easy admin edits.
  - No dependency timing issues (preferences editable even before label ingestion).
  - Suggestion code can match strings directly; no joins required.
- Cons
  - No referential integrity; typos and renames won’t be blocked.
  - Label renames in GitHub won’t auto‑propagate to preferences.
  - Harder to do SQL reporting by label vs. an M2M join.

## Operational Notes
- Validation
  - Provide a management command (or scheduled job) to compare `preferred_labels` against known labels (from `syncer.LabelDef`) and log/report unknown names.
  - Treat unknown labels as non‑matching; do not fail suggestions.
- Matching semantics
  - Case‑sensitive label name matching to mirror GitHub.
  - Preferred labels are repo‑scoped (validate within the same `Repository`).
- Migration path (if FK needed later)
  - Introduce a through table `ReviewerPreferencePreferredLabel` referencing `syncer.LabelDef`.
  - Backfill by resolving JSON names to label defs by name in the same repo; keep JSON temporarily for rollback.
  - Remove JSON after confidence is built, and switch suggestion code to the join.

## Alternatives (Optional)
- M2M to `syncer.LabelDef`
  - Pros: referential integrity, survives renames (via IDs), better admin UX.
  - Cons: couples core to syncer; preferences depend on label ingestion being present.
- Normalized core table with `label_name`
  - Pros: queryable and indexable, still decoupled from syncer.
  - Cons: still no FK/renames; adds schema surface without big gains over JSON for v1.
