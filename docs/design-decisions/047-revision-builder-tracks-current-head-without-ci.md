# Revision Builder Tracks the Current Head Even Without CI or a Force-Push Event

## Context

`PRRevision` rows are the analyzer's record of a PR's head-SHA history over
time. They are built by `rebuild_pr_revisions` (`analyzer/services/revisions.py`)
from two signals only:

1. `HEAD_FORCE_PUSHED` timeline events (`before_sha`/`after_sha`), and
2. commits that carry CI rows (`CommitCheckRun` / `CommitStatusContext`), via
   `_collect_ci_first_seen`.

`pr.head_sha` (the authoritative current head, set from the bundle's
`headRefOid`) is used only as a last-resort *seed* in `_infer_seed_sha`, and
only when no CI windows can be derived at all.

These revisions feed the queue-window builder (`queue_windows.py`), which
evaluates required CI **against the head SHA of the revision active at each
boundary** (`_head_sha_at_time`). The snapshot's separate queue-candidate path
(`queueboard_snapshot._ci_status_for_pr`) instead reads `pr.head_sha` directly.
When those two head sources disagree, the two paths disagree about queue
membership.

### The failure (leanprover-community/mathlib4#40601)

A fork PR was created at head `6ee0b95`; its required `Build` check **failed**,
correctly taking it off the queue. The author then advanced the head to
`1049e60a` with an ordinary commit push (no rebase), so:

- GitHub emitted **no** `HEAD_REF_FORCE_PUSHED` event, and
- CI for `1049e60a` was **skipped** (the changed files were outside the build
  workflows' path filters), so no `CommitCheckRun`/`CommitStatusContext` rows
  ever existed for it.

The revision builder had neither of its two signals for `1049e60a`, so it stayed
pinned to `6ee0b95`. The queue-window builder kept gating on `6ee0b95`'s failed
`Build` and reported the PR off-queue indefinitely — for 8 days — while the
snapshot's candidate path (reading `pr.head_sha = 1049e60a`, missing CI →
eligible under `no_required_failures`) listed it on-queue. Re-syncing could not
help: GitHub returns the same data every time. Under the repo's configured
`no_required_failures` gating (missing/pending CI is eligible; only an explicit
failure gates), the PR *should* be on the queue.

## Decision

Make `rebuild_pr_revisions` always reflect the PR's actual current head.

- New helper `_ensure_current_head_window`: after the force-push/CI windows are
  computed, if `pr.head_sha` differs from the last derived head, close the
  trailing window and append an open-ended window for `pr.head_sha`. No-op when
  `head_sha` is unset or already matches (the common case, including normal
  force-push PRs whose last `after_sha` is the current head).
- `rebuild_pr_revisions` now forces a rebuild (skips the noop short-circuit)
  when the trailing revision head ≠ `pr.head_sha`. A plain push with no CI does
  not advance any time-based signal past `built_through_ts`, so without this the
  noop guard would never pick the new head up.
- The incremental "append" strategy now verifies that the immutable prefix
  (windows before the current tail) still matches what we re-derive; if not, it
  falls back to a full rebuild. This is needed because a synthetic current-head
  window can later be *superseded* by a CI/force-push-derived window at a
  different time (e.g. CI finally runs for a fork head), which moves an earlier
  window's `to_ts` — something the forward-only append path would otherwise skip,
  leaving a revision coverage gap.

### Dating the boundary without a push time

GitHub does **not** expose when a commit was *pushed* to a branch. Commit
`committedDate` is the author/commit time — for a rebase or cherry-pick it can
be much earlier than the push, which would place the boundary in the past and
*over*-count queue time. So we deliberately do **not** add a `committedDate`
column.

`_current_head_change_ts` instead uses `pr.gh_updated_at` as the push-time
proxy: the push bumps the PR's `updatedAt`, and our detection of the new head is
*triggered* by that bump, so at first detection `gh_updated_at` ≈ push time and
is an *upper* bound (conservative — it under-counts rather than over-counts queue
time). To prevent drift when later comments/labels bump `updatedAt`, once a
revision window exists for the head *as a continuation* (its `from_ts` is after
the last derived window) that `from_ts` is **reused** on subsequent rebuilds
rather than recomputed. An *earlier* existing window for the same head means the
head was superseded and has since returned (a revert), so it is not reused — the
re-push's `gh_updated_at` is used instead. The value is clamped to never sit in
the future and to fall strictly after the previous window's start.

This timestamp affects only queue-*time* accounting (`first_on_queue_ts`,
`total_time_on_queue`); the on/off-queue determination is correct for any
`from_ts <= now`.

## Edge cases and limitations

- **Intermediate transient heads** (A→B→C in quick succession where B has no CI
  and no force-push event) are not recorded — there is no signal that B existed
  nor any timestamp for it. The builder correctly lands on the current head C;
  only the brief B interval is absent from history.
- **`timeline_backfill_done` precondition**: `rebuild_pr_revisions` returns early
  (`skipped`) before the head check when timeline backfill is incomplete, so a
  not-yet-backfilled PR keeps a stale head until backfill finishes.
- **Reliance on `pr.head_sha`**: the fix tracks whatever the syncer records as
  the head. This is sound for live syncs (the "newer-wins" guard that could keep
  an older `head_sha` is archive-import-only); a broader syncer bug that left
  `head_sha` stale would still mislead the builder.
- The boundary timestamps are deliberate approximations (see above); they affect
  queue-time accounting only, never the on/off-queue result.

## Consequences

- The queue-window builder and the snapshot candidate path now evaluate CI
  against the same head (`pr.head_sha`), eliminating the contradictory
  on/off-queue signals.
- For #40601 and the whole class of fork-PR-without-CI cases, a revision for the
  real current head is created; with no failing required CI on that head, the
  PR re-enters the queue under `no_required_failures`.
- When the trailing window is first created, `revision_version` bumps, so the
  queue-window sweep (`analyzer.rebuild_queue_windows_sweep`) rebuilds the
  affected windows. Subsequent rebuilds reuse the stored boundary → no churn.
- Rollout for the stuck PR: deploy, then
  `manage.py rebuild_revisions --repo leanprover-community/mathlib4 --pr 40601`
  (the queue-window sweep then picks up the revision-version bump). No migration
  or resync is required — this is a pure analyzer-side change.
