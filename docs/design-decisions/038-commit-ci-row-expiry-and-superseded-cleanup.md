# Commit CI Row Expiry and Superseded-Row Cleanup

## Context
- `CommitCheckRun` and `CommitStatusContext` rows accumulate indefinitely; there is no existing pruning path.
- Two distinct classes of redundant rows build up over time:

  **Phantom pending rows** — rows whose status/state is non-terminal (`status != COMPLETED`,
  `state = PENDING`) that GitHub will never update:
  - CI workflow renames: old check run names drop out of GitHub's `statusCheckRollup`; our
    refresh task (`syncer.refresh_pending_ci_for_repo`) stops polling after
    `SYNCER_PENDING_CI_MAX_AGE_HOURS` but never marks or removes the row.
  - Cancellation race: GitHub can deliver `IN_PROGRESS + CANCELLED` before updating status to
    `COMPLETED`. This specific case is now normalised at write time (see `004-ci-status-sources.md`),
    but pre-existing rows remain.
  - Observed in production: ~90 SHAs with `CIShaFetchState.last_result = "filtered"`, all for
    mathlib4 fork PRs. GitHub's rollup for those SHAs no longer includes the original check run
    names at all.

  **Superseded same-SHA+name rows** — when a check run is re-run on the same commit, GitHub
  issues a new `github_node_id`. The upsert path creates a new row rather than updating the old
  one (different node ID), leaving the old row behind. The same pattern applies to
  `CommitStatusContext` rows ingested via the GraphQL rollup (each new status event has a new
  `github_node_id`). The analyzer already tolerates this with in-memory latest-per-name deduping,
  but the rows accumulate and add cost to every scan.

- There are no FKs pointing into `CommitCheckRun` or `CommitStatusContext` from other models
  (only the `Repository` cascade-delete direction), so bulk deletion is safe.
  **Note (updated):** `PRQueueWindow` now holds nullable FKs into both `CommitCheckRun` and
  `CommitStatusContext` for event attribution (see `040-queue-window-event-attribution.md`).
  These use `on_delete=SET_NULL`, so bulk deletion remains safe, but the expire task must mark
  affected windows stale and enqueue their PRs for rebuild before deleting rows — see doc 040.

## Decision
- Add a periodic cleanup task `syncer.expire_stale_ci_for_repo` (fanned out by
  `syncer.expire_stale_ci_for_active_repos`) that runs four deletion passes per repository:

  **Pass 1 — stale pending check runs**
  Delete `CommitCheckRun` rows where `status != COMPLETED` and
  `COALESCE(gh_started_at, gh_completed_at, created_at) < now - SYNCER_CI_STALE_PENDING_DAYS`.

  **Pass 2 — stale pending status contexts**
  Delete `CommitStatusContext` rows where `state = PENDING` and
  `gh_created_at < now - SYNCER_CI_STALE_PENDING_DAYS`.

  **Pass 3 — superseded check runs**
  For each `(repository, head_sha, name)` group with more than one row, delete all rows except
  the canonical one (highest `gh_completed_at NULLS LAST`, then `gh_started_at NULLS LAST`, then
  `id`). No age threshold — any superseded row is safe to remove regardless of age.

  **Pass 4 — superseded status contexts**
  Same as pass 3 for `CommitStatusContext`, but restricted to rows where `rest_id IS NULL`.
  Rows ingested via the REST status history path (`rest_id` set) are intentionally append-only
  and must not be deduplicated.

- New setting `SYNCER_CI_STALE_PENDING_DAYS` (default `30`). This is deliberately well above
  the `SYNCER_PENDING_CI_MAX_AGE_HOURS` refresh window (default 48 h) so the cleanup pass
  never races with legitimate long-running CI.

- New setting `SYNCER_CI_EXPIRY_PERIOD_SECONDS` (default `86400`, i.e. daily) controls both
  the beat frequency and acts as an enable/disable gate (set to `0` to disable), following the
  same convention as `SYNCER_PENDING_CI_REFRESH_PERIOD_SECONDS` and other periodic tasks.

- The task returns a summary dict with per-pass deletion counts for observability.

## Consequences
- Phantom pending rows are eventually removed, eliminating false CI state signals and reducing
  noise in admin views.
- Superseded-row cleanup makes storage and query semantics consistent with the analyzer's
  already-correct latest-per-name read behaviour.
- The cleanup also handles any future unknown source of stuck pending rows, not just the known
  cases above.
- Passes 3+4 must use an efficient subquery or window function against the
  `(repository, head_sha)` index; a naive row-by-row approach would be too slow for large repos.
- REST history rows (`rest_id IS NULL` guard in pass 4) are explicitly preserved; removing them
  would destroy historical StatusContext data for repos that have history mode enabled.

## Operational Notes
- `SYNCER_CI_STALE_PENDING_DAYS` defaults to `30`; can be raised per environment if longer CI
  jobs are expected. Setting it to `0` disables passes 1 and 2.
- The initial run against the existing backlog (~93 stale phantom rows on mathlib4) is the main
  motivation; subsequent runs will be low-volume.
- Beat schedule: `syncer.expire_stale_ci_for_active_repos` is gated on
  `SYNCER_CI_EXPIRY_PERIOD_SECONDS > 0` and runs at that interval (default daily). Set to `0`
  to disable entirely.
- No migration needed; the task operates on existing tables.
- The `CIShaFetchState` ledger is not cleaned up by this task. Stale ledger rows for deleted
  SHAs are harmless (they simply gate re-enqueue attempts that no longer apply) and can be
  addressed separately if ledger table growth becomes a concern.

## Alternatives
- **Filter at query time only** — the analyzer already does this (latest-per-name). Rejected as
  sole strategy: storage still bloats, scans get slower, and phantom pending rows pollute admin
  views and convergence metrics regardless of read-layer deduping.
- **Update phantom pending rows to COMPLETED with a synthetic conclusion** (e.g. `STALE`) —
  requires adding `STALE` to the `CheckRunConclusion` enum and complicates downstream logic that
  must now distinguish synthetic from real conclusions. Deletion is simpler and has the same
  observable effect.
- **Separate tasks per model** — no benefit; the four passes share a threshold setting, a fan-out
  pattern, and a return-dict shape. A single task is easier to schedule and monitor.

## References
- `docs/design-decisions/004-ci-status-sources.md` — status/conclusion normalisation
- `docs/design-decisions/019-ci-by-sha-ledger-and-sha-keyed-ci.md` — `CIShaFetchState` backoff ledger
- `qb_site/syncer/models/commit_check_run.py`
- `qb_site/syncer/models/commit_status_context.py`
- `qb_site/syncer/tasks/sync_tasks.py` — `refresh_pending_ci_for_repo_task` pattern to mirror
