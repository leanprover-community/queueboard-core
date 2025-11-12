# Analyzer Plan: Queue Windows, Historical Snapshots, and Backfill

This plan captures how the Analyzer app will compute "who was on the queue at time T" and metrics like "total time on the queue" while keeping token usage bounded. It builds on the Syncer v1 ingestion (labels, key timeline events, CI snapshots) without adding heavy state early.

## Goals
- Compute queue membership at an instant T and over an interval [T0, T1].
- Compute per‑PR queue windows (enter/exit) and aggregate durations.
- Support historical backfills for dates before the scheduler was running, efficiently.

## Definitions (initial)
- Queue membership rules (subject to refinement/config):
  - PR is open (not closed/merged) and not draft.
  - PR has required queue labels (e.g., `prio:high`, `waiting-for-review`, etc.).
  - Optional: PR passes CI (derive from Analyzer events below); rules configurable per repo.

## Data Inputs
- `syncer.PullRequest`: createdAt, updatedAt, closedAt, mergedAt, is_draft, base/head refs.
- `syncer.PRTimelineEvent`: label add/remove, draft toggles, reopen/closed, head ref force‑push (already modeled).
- `syncer.CheckRun` and `syncer.StatusContext`: snapshots per head commit via statusCheckRollup.
- Derived (Analyzer): `PRCIStatusEvent` stream (pass/fail/running/missing) from the above snapshots.

## Core Outputs
- Queue windows per PR: list of [enter_at, exit_at) intervals with reason(s).
- Instant set at T: PRs whose window contains T.
- Duration metrics: total time on queue per PR over [T0, T1]; aggregations per repo/author/day.

## Services (Analyzer)
1) `queue_rules.py` (config + helpers)
   - Encapsulate per‑repo label sets and CI rules.
   - Provide predicates: `is_open_not_draft(pr_at_t)`, `has_required_labels(labels_at_t)`, `ci_ok(ci_state_at_t)`.

2) `label_state_builder.py`
   - Build label presence intervals from `PRTimelineEvent` add/remove events (case preserved, normalized compare).
   - Expose: `labels_active_at(t)`, `label_intervals(name)`.

3) `ci_events.py`
   - Transform `CheckRun`/`StatusContext` snapshots into coarse `PRCIStatusEvent` transitions (pass/fail/running/missing), using repo config for inessential jobs.
   - Expose: `ci_state_at(t)`, `ci_windows()`.

4) `queue_window_builder.py`
   - Combine open/not‑draft intervals, required label intervals, and optional CI pass intervals to produce queue windows.
   - Expose: `queue_windows(pr)`, `contains(t)`, `duration_between(t0, t1)`.

5) Query utilities
   - `who_was_on_queue_at(repo, t)` → list of PRs and reasons.
   - `queue_time_by_pr(repo, t0, t1)` → per‑PR durations; `queue_time_aggregate(repo, t0, t1)` → totals.

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

## Preliminaries Implemented (now)
- Client query: `prs_created_page.graphql` and `GitHubClient.get_prs_created_page(...)`.
- Timeline override plumbing: `PRSyncService.sync_pull_request(..., timeline_since_iso_override=...)` and `sync_pr_task(..., timeline_since_iso=...)` to ingest history from a requested cutoff (used by backfills).

## Next Steps (incremental work)
1) Analyzer services skeletons with docstrings and TODOs; unit tests with synthetic fixtures.
2) Management command `backfill_window` (Analyzer):
   - `--repo owner/name --from ISO --to ISO [--include-ci] [--limit N]`.
   - Stage A: enumerate candidates via created‑at pages.
   - Stage B: enqueue `syncer.sync_pr` tasks with `timeline_since_iso` and optional commit paging caps.
   - Optional: rate‑aware batching; resume at `resetAt`.
3) Optional model: `PRCIStatusEvent` (if not yet present) with compact transitions; small test to validate window derivation.
4) Admin/CLI utilities: quick reporting of "who was on the queue at T" using DB‑only reads after backfill.

## Scheduling Notes
- Keep per‑repo periodic sync in Syncer beat schedule (OPEN since recent cutoff).
- Run backfills ad‑hoc via the Analyzer command or a temporary periodic task disabled by default.

## Testing Strategy
- Unit: interval builders (labels, CI) with edge cases (boundary equality, overlapping events).
- Integration: backfill flow over small fixtures; snapshot queue sets at known timestamps.
- Performance: sanity check candidate discovery page counts and bundle volumes on a large repo.

