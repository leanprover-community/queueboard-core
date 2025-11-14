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
   - `ci_state_at_time(pr, T)` helper (essentials only; unknown‑CI policy via rules).
   - `queue_state_at_time(repo, T)` combines open/not‑draft + required labels + CI rules.
   - CLI for “who was on the queue at T” and sampling utilities.
3) Daily results and rules versioning
   - Models: `QueueDailySnapshot` and `PRQueueDailySpan`, stamped with `rules_version`.
   - Batch jobs to compute EOD snapshots and backfill ranges (idempotent upserts).
   - `QueueRuleSet` model (per‑repo, versioned) and admin to manage rule changes going forward.
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

## Planned: Head Revision Windows and CI Backfill Across Force-Pushes

To make CI-at-time reconstructions robust to force-pushes, we plan to anchor CI history to head-SHA windows.

### Model (Analyzer)
- `PRRevision`:
  - Fields: `pr` (FK), `head_sha` (str), `from_ts` (datetime), `to_ts` (nullable datetime), `seq` (int, descending by time).
  - Built from timeline `HEAD_REF_FORCE_PUSHED` events and header fields; the first window seeds from PR header at `createdAt`.
- Optional `CommitCIRollup` (if/when we want compact per-context records):
  - Unique by `(repo_id, sha, context_key)`; fields include `status`, `conclusion`, `createdAt/startedAt/completedAt` and `source`.

### Flow
1) Build/refresh `PRRevision` after Syncer ingests timeline pages for a PR.
2) Identify the next N historical SHAs missing CI and enqueue Syncer requests to fetch CI for those SHAs (steady, budgeted progress).
3) For a query at time T:
   - Resolve head SHA via the revision window containing T.
   - Evaluate labels/open/draft from timeline intervals as of T.
   - Read CI state as of T for that SHA from snapshots/rollups and apply repo rules.

### Coordination
- Keep Syncer autonomous for rate budgeting; Analyzer writes requests (or directly enqueues tasks) and lets Syncer apply rate guards and continuation.
- Requests should be idempotent/deduplicated (unique `(repo, sha, kind)`), with optional `not_before=resetAt` for polite rescheduling.

### Deliverables
- Models + migrations for `PRRevision` (and optional rollups).
- Services with clear docstrings and small, focused tests.
- A lightweight admin or CLI to inspect revision windows and probe CI-at-time for a PR at T.
