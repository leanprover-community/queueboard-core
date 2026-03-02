# PRRevision Incremental Build-State and Segment CI Harvest

## Context
- `PRRevision` windows are rebuilt from scratch each time, even when only the tail changes, which wastes work on stale PRs.
- Analyzer sometimes needs heads we never observed live (e.g., pre-rebase commits) to place CI-gated queue windows correctly.
- CI-first-seen timestamps refine window boundaries; late-arriving CI can shift earlier boundaries and must not silently corrupt history.
- We need a correctness-first guardrail with a fast path for strictly-forward updates, plus a way to discover heads inside force-push segments without prior snapshots.

## Decision
- Add `analyzer.PRRevisionBuildState` (OneToOne → `syncer.PullRequest`) to track `built_through_ts`, `dirty_from_ts`, `builder_version`, `last_built_at`, and optional tail pointers; leave `PRRevision` rows as pure windows and keep `seq` as a derived ordering helper.
- Rebuild modes:
  - If state is missing, `builder_version` mismatches, `dirty_from_ts` is set, or signals arrive < `built_through_ts`, run a full recompute (existing semantics) and renumber windows.
  - If state is clean and all new signals are strictly after `built_through_ts`, close the tail window and append new ones in a single transaction (tail append).
- Head discovery is timeline-anchored:
  - Use force-push segments as anchors; for each segment, harvest commits by walking history from the segment head back to the segment start sentinel.
  - Candidate heads = timeline before/after SHAs + harvested commits + already-seen CI heads; dedupe blanks.
  - Enqueue `syncer.sync_ci_for_shas` for candidates lacking CI; allow `require_pr_association=false` for historical heads.
- Rebuild uses earliest CI per head to split windows; if a candidate lacks CI, approximate with commit timestamps inside its segment. CI arriving earlier than `built_through_ts` marks the PR dirty to force a full recompute.
- Introduce a small per-PR orchestrator task (`analyzer.process_pr` style) that:
  - Skips if timeline backfill is incomplete.
  - Harvests segment commits when needed (Syncer-owned SHA-first history fetch).
  - Enqueues missing CI and exits.
  - Runs rebuild (full or append based on state) and returns whether the change was full or tail-only so queue windows can be rebuilt appropriately.

## Consequences
- Pros: avoids reprocessing stale history, keeps correctness via dirty/full fallback, and can recover rewritten-away heads when reachable via segment commit harvest. Keeps Syncer schema untouched and Analyzer concerns isolated in its own table.
- Cons: extra Analyzer table/state and an orchestrator loop; commit harvest/API calls add cost and depend on GitHub retaining the commits. Missing or unreachable commits still limit reconstruction; we lean on CI-first-seen when available.
- Behavior is deterministic per `builder_version`; bumping the version forces full recompute and window renumbering.

## Operational Notes
- Ingestion hooks (timeline/CI writes) update build-state: if a signal timestamp < `built_through_ts`, set `dirty_from_ts` (earliest seen); otherwise no-op. This avoids repeated data scans.
- Update (2026-03-02): CI ingestion now applies a stability guard before setting dirty:
  - `sync_check_runs` / `sync_status_contexts` only treat CI as a dirtying signal when a snapshot row is newly created or when revision-relevant evidence changed (head SHA or CI timestamps).
  - Re-observing unchanged historical CI snapshots should not repeatedly set `dirty_from_ts`.
  - When a payload contains multiple rows for the same CI name, dirtying now considers only the newest row per name. Older rows may still be ingested for bookkeeping, but they are pruned and no longer trigger repeated dirtying.
  - Rationale: preserve correctness for genuinely new earlier evidence while preventing revision-version churn from idempotent re-syncs.
- Update (2026-03-02): Timeline ingestion now marks revisions dirty only for revision-relevant event types (currently `HEAD_FORCE_PUSHED`). Non-revision timeline events (labels/assignment/state toggles) are still ingested but no longer cause revision rebuild dirtying.
- Use per-PR advisory locks for the orchestrator to prevent overlap. Timeline not backfilled → defer rather than churn.
- Queue windows: full revision rebuild → full queue window rebuild for the PR/ruleset; tail append → rebuild only the tail windows.
- Keep `seq` derived; identity remains `(pull_request, from_ts)`. If mid-history changes are needed, rely on dirty/full recompute rather than trying to insert in-place.
- Commit history harvest is owned by Syncer with a resumable cursor (e.g., `(pull_request, start_sha, cursor, has_more, cutoff_ts)`); low page limits converge via repeated harvest tasks/sweeps without Analyzer owning raw fetches. Syncer harvest tasks also enqueue CI for harvested heads missing CI.

## Alternatives
- Store build-state on `syncer.PullRequest`; rejected to keep Analyzer metadata out of the raw ingest schema.
- Keep only full rebuilds; rejected to avoid unnecessary work on stale PRs and to make tail updates cheaper.
- Drive CI backfill solely from existing `PRRevision` heads; rejected because it cannot discover pre-rewrite heads like “a” when the PR is first seen after a force-push. The segment commit harvest closes that gap. 
