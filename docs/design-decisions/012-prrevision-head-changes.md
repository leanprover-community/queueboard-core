# PRRevision Refinement: Head Changes Beyond Force-Pushes

## Context
- `analyzer.PRRevision` models head windows per PR:
  - Each row represents a contiguous interval `[from_ts, to_ts)` during which the PR's head SHA was `head_sha`.
  - Windows are built from `syncer.PRTimelineEvent` `HEAD_FORCE_PUSHED` events combined with CI / commit evidence about head changes:
    - Force-push events define hard segment boundaries and baseline heads.
    - Within each segment, earliest CI timestamps per `head_sha` introduce additional revision windows when the head advances without a force-push.
    - When no force-push events exist for a PR, windows are inferred solely from CI snapshots grouped by `head_sha`, with a best-effort seed from the latest CI snapshot when CI is sparse.
- Queue windows for CI-gated rulesets use PRRevision to:
  - Resolve the head SHA at time `T`.
  - Add revision boundaries as potential queue window boundaries.
- Syncer backfills commits and CI per SHA over time, but:
  - Not every head change is accompanied by a `HEAD_FORCE_PUSHED` event.
  - In many repos, heads may advance via normal pushes without recorded force-pushes.
- We want queue semantics that reflect meaningful head changes (different commits) without requiring a schema rewrite:
  - CI gating should depend on the head that was actually current at time `T`.
  - Queue windows should split when the PR's head truly changes, not only when a force-push occurs.

## Decision
- Keep the core semantics of `PRRevision`:
  - A row still means: “this head SHA was current for the PR over `[from_ts, to_ts)`”.
  - The `PRRevision` table remains the single source of truth for head windows used by Analyzer.
- Refine the revision builder logic (`rebuild_pr_revisions`) to consider additional signals beyond `HEAD_FORCE_PUSHED`:
  - Continue to treat `HEAD_FORCE_PUSHED` events as hard boundaries.
  - Incorporate other head-change evidence where possible (e.g., commits/CI data indicating the PR’s head SHA has advanced) to create additional revision windows.
  - Maintain non-overlapping windows ordered by time, preserving the [from_ts, to_ts) invariant.
- Do not change the schemas for `PRRevision`, `CheckRun`, or `StatusContext`:
  - Use existing `head_sha` and provider timestamps to infer additional revision boundaries.
  - Rely on recomputation of `PRRevision` and `PRQueueWindow` rather than a data migration that mutates stored windows in place.

## Consequences
- Pros
  - CI gating per time `T` becomes more accurate:
    - `is_on_queue_at(pr, T)` and CI-gated queue windows can distinguish CI for old vs new heads when the head changes without a force-push.
  - Queue windows that rely on PRRevision naturally become finer-grained where the head changes more often.
  - No schema rewrite is required; changes are confined to Analyzer services and backfill pipelines.
- Cons
  - `PRRevision` and `PRQueueWindow` row counts will grow for long-lived PRs with many head changes.
  - Historical windows for PRs that are recomputed will change as we improve the revision builder; analysts must be aware that earlier approximations may be superseded by more accurate windows.

## Operational Notes
- Implementation steps
  - Extend `analyzer.services.revisions.rebuild_pr_revisions` to:
    - Continue to use `HEAD_FORCE_PUSHED` events as primary, hard boundaries.
    - Within each segment:
      - Before the first force-push (from `gh_created_at` to first `HEAD_FORCE_PUSHED.occurred_at`),
      - Between successive force-push events, and
      - After the last force-push (from last `HEAD_FORCE_PUSHED.occurred_at` onward),
      incorporate additional head-change signals (e.g., CI/commit data) to infer when the head SHA changes and create additional `PRRevision` windows for those heads.
    - Preserve idempotency and atomic replacement of the window set per PR.
  - Status:
    - Implemented in `qb_site/analyzer/services/revisions.py`:
      - `_collect_ci_first_seen`, `_build_ci_head_windows`, and `_build_force_push_head_windows` combine force-push segments with CI-derived head changes.
      - `rebuild_pr_revisions` now uses these helpers for both force-push and no-force-push cases.
    - Tested via `qb_site/analyzer/tests/test_pr_revisions.py`, including multi-segment scenarios with both baseline and non-baseline CI heads.
  - Ensure that `PRRevision` remains rebuildable end-to-end from Syncer tables (`PullRequest`, `PRTimelineEvent`, `CheckRun`, `StatusContext`).
- Recompute strategy
  - Treat `PRRevision` and `PRQueueWindow` as derived artifacts:
    - After refining `rebuild_pr_revisions`, re-run:
      - `rebuild_revisions` for targeted PRs or repos.
      - `analyzer.process_pr` (or a dedicated Analyzer backfill task) to rebuild queue windows under each relevant `QueueRuleSet`.
    - Do not attempt to patch existing revisions/windows in place; delete and recompute for affected PRs/rulesets.
- Approximations that remain
  - We still do not attempt to reconstruct CI flapping on the same SHA:
    - CI gating uses the latest available snapshot per context per head SHA as of time `T`.
  - CI history for missing/expired contexts remains unrecoverable:
    - CI-gated rulesets will show no windows where CI cannot be established; legacy label-only rulesets cover those PRs instead (see `011-ci-gating-and-legacy-prs.md`).

## Alternatives (Optional)
- **Introduce a separate "PRHeadChange" table**
  - Pros: clearer separation of head-change logic; explicit storage for non-force-push head changes.
  - Cons: additional schema and ingestion complexity; requires merging a second stream when reconstructing timelines.
- **Compute head windows on the fly without PRRevision**
  - Pros: avoids another table.
  - Cons: repeated expensive work on every queue computation; less control over backfill and visibility in admin.
