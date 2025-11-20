# Analyzer Plan: Queue Windows, Historical Snapshots, and Backfill

This plan captures how the Analyzer app will compute "who was on the queue at time T" and metrics like "total time on the queue" while keeping token usage bounded. It builds on the Syncer v1 ingestion (labels, key timeline events, CI snapshots) without adding heavy state early. See also `docs/design-decisions/010-queue-windows-first.md` for the decision to ship queue windows first and defer other interval tables.

## Goals
- Compute queue membership at an instant T and over an interval [T0, T1].
- Compute per‑PR queue windows (enter/exit) and aggregate durations.
- Support historical backfills for dates before the scheduler was running, efficiently.

## Definitions (initial)
- Queue membership rules (per‑repo, versioned via `QueueRuleSet`):
  - PR is open (not closed/merged) and not draft.
  - PR has all required queue labels (e.g., `prio:high`, `waiting-for-review`, etc.) and none of the forbidden labels.
  - PR passes required CI contexts for that ruleset (derived from Analyzer CI helpers; wiring is incremental).

## Data Inputs
- `syncer.PullRequest`: createdAt, updatedAt, closedAt, mergedAt, is_draft, base/head refs.
- `syncer.PRTimelineEvent`: label add/remove, draft toggles, reopen/closed, head ref force‑push (already modeled).
- `syncer.CheckRun` and `syncer.StatusContext`: snapshots per head commit via statusCheckRollup.
- Derived (Analyzer, planned): `PRCIStatusEvent` stream (pass/fail/running/missing) from the above snapshots.

## Core Outputs
- Queue windows per PR: list of [enter_at, exit_at) intervals with reason(s).
- Instant set at T: PRs whose window contains T.
- Duration metrics: total time on queue per PR over [T0, T1]; aggregations per repo/author/day.

## Services (Analyzer)

1) `queue_rules.py` (config + helpers) — **implemented**
   - Encapsulates per‑repo queue rules and CI requirements, backed by `analyzer.QueueRuleSet`.
   - Provides predicates via `QueueRules.is_on_queue(...)`, combining:
     - `require_open` / `require_not_draft`.
     - `required_label_names` (all must be present) and `forbidden_label_names` (none may be present).
     - `require_ci_success` and `required_ci_contexts` (CI gating to be wired via helpers).
   - Helpers:
     - `load_rules_for_repo(repo, at)` → selects the best matching ruleset for time `at` using `effective_from` / `effective_to`, with fallback to the latest version.
     - `rules_for_rule_set(rule_set)` → in‑memory rules for a specific `QueueRuleSet`.

2) `label_state_builder.py` — **planned, deferred**
   - Build label presence intervals from `PRTimelineEvent` add/remove events (case preserved, normalized compare).
   - Expose: `labels_active_at(t)`, `label_intervals(name)`.
   - Deferred per `010-queue-windows-first`: initial rollout will replay labels directly from `PRTimelineEvent` inside the queue window builder.

3) `ci_events.py` — **planned**
   - Transform `CheckRun`/`StatusContext` snapshots into coarse `PRCIStatusEvent` transitions (pass/fail/running/missing), using repo config and ruleset CI requirements.
   - Expose: `ci_state_at(t)`, `ci_windows()`.

4) `queue_window_builder.py` — **implemented (first version, CI-aware)**
   - Implemented as `analyzer.services.queue_windows`:
     - In‑memory helpers compute queue windows directly from `PullRequest` + `PRTimelineEvent` + `PRRevision` + CI snapshots and `QueueRules`:
       - `queue_windows_for_pr(pr, as_of)` → `[enter, exit)` windows.
       - `total_queue_time_for_pr(pr, as_of)` → total seconds on queue.
       - `is_on_queue_at(pr, at)` → membership at instant `T`.
       - `who_was_on_queue_at(repo, at)` → PRs whose window contains `T` under the repo’s latest ruleset.
     - Persistence helper:
       - `rebuild_queue_windows_for_ruleset(pr, rule_set, as_of)` → writes `PRQueueWindow` rows keyed by `(pr, rule_set, from_ts)` with `cycle_index`.
   - CI gating is layered into:
     - `is_on_queue_at(pr, T)` via `PRRevision` (head SHA at T) and CI snapshots for required contexts.
     - CI‑gated queue windows (when `require_ci_success=True`) via a combined timeline of label/draft/open/closed events, revision boundaries, and CI event times.

5) Query utilities — **partially implemented**
   - Instant membership:
     - `who_was_on_queue_at(repo, t)` implemented via `queue_windows.is_on_queue_at`.
   - Per‑PR durations:
     - `queue_time_by_pr(repo, t0, t1)` and `queue_time_aggregate(repo, t0, t1)` remain planned; initial version focuses on:
       - `total_queue_time_for_pr(pr, as_of)` and
       - `PRQueueWindow` windows (with `cycle_index`) for cycle‑count analysis.

## Historical Backfill Flow
Backfills need detail around T for PRs that could have been on the queue. Avoid per‑PR bundles until we confirm candidates.

Stage A: Candidate discovery (header/minimal pages)
- Add GraphQL listing ordered by `CREATED_AT ASC` with nodes `{ number, createdAt, closedAt, mergedAt, isDraft, state }` (implemented in `syncer/queries/prs_created_page.graphql`).
- Page until `createdAt > T_end` and filter: `createdAt <= T_end` and `(closedAt is null or closedAt > T_start)`.

Stage B: Targeted detail ingest
- For candidates, fetch PR bundle with explicit `timeline_since = T_start - epsilon` and enable timeline paging; persist label/draft/reopen/close events.
- CI backfill only if queue rules require CI gating: increase `commitsM` and allow capped commit pages to capture status near T.

Stage C: Compute windows and sets
- Build label/CI/open/draft intervals and derive queue windows. Compute set at T and durations over [T0, T1].

## Current Preliminaries
- Client query: `prs_created_page.graphql` and `GitHubClient.get_prs_created_page(...)`.
- Timeline override plumbing: `PRSyncService.sync_pull_request(..., timeline_since_iso_override=...)` and `sync_pr_task(..., timeline_since_iso=...)` to ingest history from a requested cutoff (used by backfills).
- Head revision windows are implemented:
  - Model: `analyzer.PRRevision(pr, head_sha, from_ts, to_ts, seq)` with indexes for time and SHA.
  - Service: `analyzer.services.revisions.rebuild_pr_revisions(pr)` builds windows from force‑push events (seeding from CI when no events exist) and replaces rows atomically.
  - Targeting: `analyzer.services.revisions.next_revision_backfill_shas(pr, limit)` returns head SHAs missing any CI in Syncer tables.
  - Tests: see `qb_site/analyzer/tests/test_pr_revisions.py`.
- Syncer support for historical CI by SHA exists and is rate‑aware:
  - Query: `syncer/queries/ci_by_commit.graphql` (includes `associatedPullRequests` for optional safeties).
  - Service/Task: `syncer.services.ci_by_sha_service.sync_ci_for_sha`, `syncer.tasks.sync_tasks.sync_ci_for_shas`.
  - Admin tool: "Enqueue CI by SHA" under PRs (with an optional strict association guard).

 - Queue rules and windows (Analyzer) are implemented:
  - Models:
    - `analyzer.QueueRuleSet` (per‑repo, versioned queue rules) with:
      - `require_open`, `require_not_draft`, `require_ci_success`.
      - `required_label_names`, `forbidden_label_names` (label gates).
      - `required_ci_contexts` (CI contexts this ruleset requires).
      - `effective_from`, `effective_to` to scope which PRs/time ranges a ruleset is intended to cover (e.g., legacy label-only vs CI-gated eras).
    - `analyzer.PRQueueWindow` (per‑PR, per‑ruleset queue windows):
      - `pull_request`, `rule_set`, `from_ts`, `to_ts`, `cycle_index` (window index per `(pr, rule_set)`).
  - Services:
    - `analyzer.services.queue_rules` to materialize `QueueRules` from `QueueRuleSet` (or defaults) at a given time.
    - `analyzer.services.queue_windows`:
      - In‑memory queue windows and membership helpers (see above), CI- and PRRevision-aware for CI-gated rulesets.
      - `rebuild_queue_windows_for_ruleset` to persist windows to `PRQueueWindow` for a given `(PR, QueueRuleSet)`, gated on `timeline_backfill_done` and PRRevision presence for CI-gated rulesets.
  - Tests:
    - `qb_site/analyzer/tests/services/test_queue_windows.py`.
    - `qb_site/analyzer/tests/services/test_queue_window_model.py`.
    - `qb_site/analyzer/tests/services/test_queue_windows_ci.py`.
    - `qb_site/analyzer/tests/services/test_queue_windows_prrevision.py`.
    - `qb_site/analyzer/tests/services/test_queue_window_ci_windows.py`.
    - `qb_site/analyzer/tests/services/test_queue_window_gating.py`.
    - `qb_site/analyzer/tests/services/test_queue_rules_effective_bounds.py`.

## Current Admin & Commands
- Admin
  - Read‑only PRRevision list view (searchable by PR number/head SHA; date hierarchy on from_ts).
  - PR detail shows PRRevision inline (read‑only) alongside timeline, check runs, and status contexts.
  - PR detail object tools:
    - Analyzer: Rebuild revisions (gated on `timeline_backfill_done`).
    - Analyzer: Enqueue missing CI (uses revision windows to select missing head SHAs and enqueues Syncer CI‑by‑SHA).
- Commands
  - `manage.py rebuild_revisions --repo o/name [--pr N ...]` — rebuilds PRRevision windows; skips PRs without full timeline backfill; idempotent reconcile.
  - `manage.py plan_ci_backfill --repo o/name [--pr N ...] [--limit M] [--pages-per-sha K] [--enqueue] [--require-assoc]` — lists/enqueues CI‑by‑SHA for missing revision heads.

## Planned Work (incremental)
1) Coordinator (optional periodic)
   - Periodically rebuild revisions for PRs with recent timeline changes.
   - Identify `next_revision_backfill_shas(pr)` and enqueue limited `syncer.sync_ci_for_shas` per PR under rate‑aware caps.
2) CI state and queue queries
   - `ci_state_at_time(pr, T)` helper (essentials only; unknown‑CI policy via rules and `required_ci_contexts`).
   - Refine `queue_state_at_time(repo, T)` / `who_was_on_queue_at` to incorporate CI gating once CI helpers are in place.
   - CLI for “who was on the queue at T” and sampling utilities, backed by `PRQueueWindow` where available and in‑memory computation otherwise.
3) Daily results and rules versioning
   - Models: `QueueDailySnapshot` and `PRQueueDailySpan`, stamped with `rules_version` / `QueueRuleSet`.
   - Batch jobs to compute EOD snapshots and backfill ranges (idempotent upserts).
   - Admin and tooling for `QueueRuleSet` (queue rules) and `PRQueueWindow` (per‑PR windows and cycles).
4) Optional: compact CI rollups
   - If needed for speed, add `CommitCIRollup` to store a latest record per `(repo, sha, context)` to accelerate historical lookups.

## Scheduling Notes
- Keep per‑repo periodic sync in Syncer beat schedule (OPEN since recent cutoff).
- Run backfills ad‑hoc via the Analyzer command or a temporary periodic task disabled by default.

## Integration Notes
- Association guard and force‑pushes
  - GitHub’s `associatedPullRequests` reflects current inclusion; after a force‑push, older heads may no longer appear even though they were part of the PR historically.
  - For Analyzer‑driven backfills, pass `require_pr_association=false` to `syncer.sync_ci_for_shas` and instead verify that the SHA belongs to the PR via `PRRevision`.
- Rate‑awareness remains in Syncer
  - Analyzer only enqueues work; Syncer’s task will guard on `SYNCER_RATE_REMAINING_MIN` and schedule continuation at `resetAt`.

## Testing Strategy
- Unit: interval builders (labels, CI) with edge cases (boundary equality, overlapping events).
- Integration: backfill flow over small fixtures; snapshot queue sets at known timestamps.
- Performance: sanity check candidate discovery page counts and bundle volumes on a large repo.

## Planned: PRRevision Refinement and CI Backfill Across Head Changes

To make CI-at-time reconstructions robust to more than just force-pushes, we plan to refine head revision windows and CI backfill while keeping `PRRevision` as the canonical "head window" model.

### Model (Analyzer)
- `PRRevision` (already implemented):
  - Fields: `pr` (FK), `head_sha` (str), `from_ts` (datetime), `to_ts` (nullable datetime), `seq` (int).
  - Built from timeline `HEAD_REF_FORCE_PUSHED` events plus CI-derived head changes:
    - When force-push events exist, they define hard segment boundaries and baseline heads; within each segment, earliest CI timestamps per new head SHA introduce additional revision windows.
    - When no force-push events exist, windows are inferred from CI snapshots grouped by `head_sha`, with a fallback to seeding from the most recent CI snapshot head if necessary.
- Optional `CommitCIRollup` (if/when we want compact per-context records):
  - Unique by `(repo_id, sha, context_key)`; fields include `status`, `conclusion`, `createdAt/startedAt/completedAt` and `source`.
- `PRRevisionBuildState` (new, Analyzer-owned, OneToOne → `syncer.PullRequest`):
  - Tracks `built_through_ts`, `dirty_from_ts`, `builder_version`, `last_built_at`, and optional tail pointers.
  - Drives the choice between a full recompute (dirty/version mismatch/late data) and a tail append (strictly forward-only signals).

### Flow (target state)
1) **Refine PRRevision windows (stateful)**
   - Keep non-overlapping `[from_ts, to_ts)` semantics. If state is missing, dirty, or version-mismatched, run a full recompute and renumber windows. If signals are strictly after `built_through_ts`, close the tail window and append atomically.
2) **Discover candidate heads per segment**
   - Anchor on timeline force-push segments. For each segment, harvest commits by walking history from the segment head back to the segment start sentinel; add timeline before/after SHAs and already-seen CI heads. These candidates feed CI backfill even if never observed live.
3) **Drive CI backfill per candidate head**
   - Enqueue `syncer.sync_ci_for_shas` for candidates lacking CI (earliest-first). CI arriving earlier than `built_through_ts` marks the PR dirty; later CI allows tail append.
4) **Compute CI-aware queue state at time T**
   - Resolve head SHA via `PRRevision`; evaluate labels/open/draft; evaluate CI state for required contexts; apply `QueueRuleSet`.
5) **Rebuild CI-gated queue windows**
   - Full revision rebuild → rebuild all queue windows for the PR/ruleset. Tail append → rebuild only tail windows, preserving cycle indices.

### Coordination
- Keep Syncer autonomous for rate budgeting; Analyzer enqueues `sync_ci_for_shas` and runs a small per-PR orchestrator task (with an advisory lock) that:
  - Skips until timeline backfill is complete.
  - Harvests segment commits when needed.
  - Enqueues missing CI and exits when waiting on CI.
  - Runs rebuild (full vs append based on build-state) and reports whether queue windows need full or tail rebuild.
- Any signal (timeline/CI) with timestamp < `built_through_ts` marks state dirty to force a full recompute on the next orchestrator pass.
- Requests remain idempotent and deduplicated per `(repo, sha, kind)`, with optional `not_before=resetAt` for polite rescheduling (as described in `docs/syncer_ingestion_plan.md`).

### Deliverables
- Refined `rebuild_pr_revisions` implementation with tests that cover:
  - Force-push only PRs.
  - PRs with head changes inferred from CI/commits without explicit `HEAD_REF_FORCE_PUSHED` events.
- Rebuild commands/tasks:
  - Use existing `rebuild_revisions` and `analyzer.process_pr` (or a dedicated Analyzer backfill task) to recompute `PRRevision` and `PRQueueWindow` for affected PRs.
- Updated docs:
  - `docs/design-decisions/012-prrevision-head-changes.md` describing the refined semantics and recompute strategy.
  - `docs/design-decisions/013-prrevision-incremental-build-state.md` describing the incremental build-state, commit harvest, and orchestrator plan.
