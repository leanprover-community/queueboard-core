# CI-Write Watermark for Queue-Window Staleness

## Context

Queue windows (`PRQueueWindow`) are *materialized*: `rebuild_queue_windows_for_pr`
reads CI rows at build time, evaluates the ruleset's gating, and bakes the
result into `PRQueueWindow` rows (with attribution FKs to specific
`CommitCheckRun` / `CommitStatusContext` rows that triggered each
open/close). After materialization, the rows do not auto-update when CI
evolves.

The materialized data feeds:

- **on/off-queue determination** in
  `analyzer/services/queueboard_snapshot.py:762–765`. The snapshot derives
  `current_status: "OnQueue" | "OffQueue"` from the tail
  `PRQueueWindow.from_ts` / `to_ts`, not from a fresh CI evaluation.
- **`first_on_queue_ts`, `total_time_on_queue`, sort key for the queue
  board**: rollup fields on `PRQueueWindow`, computed at materialization.
- **`PRQueueWindowBuildState.windows_built_at`**: the per-(PR, ruleset)
  watermark of "windows materialized at this time."

(The CI status *badge* on each PR card is computed fresh by
`_ci_status_for_pr` at snapshot-build time — `queueboard_snapshot.py:457`.
This freshness is independent of the materialized windows.)

So if CI evolves on a SHA after `rebuild_queue_windows_for_pr` last ran
for the affected PR — a re-run flips fail → pass, a previously-pending
context completes, a webhook delivers a new conclusion — the materialized
windows stay stale until the queue builder runs again. The displayed CI
badge updates next snapshot build, but the on/off-queue decision and the
queue-time sort do not.

## The bug

The mechanism that should signal "queue windows might be stale" is
`mark_pr_revision_dirty_if_earlier(pr, signal_ts)` in
`analyzer/services/revisions.py:32–54`, called by the CI sub-syncs at
`syncer/services/sub/ci_sync.py:273` (`sync_check_runs`) and `:352`
(`sync_status_contexts`). The helper sets `PRRevisionBuildState.dirty_from_ts`,
which feeds the rebuild sweeps. It bails in two cases:

1. `built_through_ts is None` (`revisions.py:46`) — no dirty marker for a
   PR whose revision builder has not yet recorded a successful build.
2. `signal_ts >= built_through_ts` (`revisions.py:48`) — no dirty marker
   when the new CI signal is at or after the last build's high-water mark.

Case (2) is the practical one: a CI re-run completes *after* the previous
revision build's `built_through_ts`. The helper is a no-op. The revision
builder isn't re-invoked, and even if it were, `_collect_ci_first_seen` is
keyed on `head_sha` and tracks the *earliest* CI per SHA — a re-run is a
later event on the same SHA, so `windows_changed = False`,
`revision_version` is not bumped, and the queue-window sweep's predicate
(`rebuild_queue_windows_sweep.py:20–82`) finds nothing stale.

The queue-window sweep predicate today checks ruleset `updated_at`,
`pr.gh_updated_at`, `revision_version`/`revision_version_built` mismatch,
and a few backfill flags. **It does not check CI write times.** So
CI-only changes are invisible to it.

This bug is mostly masked in production because `sync_pr_task`
unconditionally enqueues `process_pr_task` after every successful per-PR
sync (`syncer/tasks/sync_tasks.py:524–533`), and `process_pr` calls
`rebuild_queue_windows_for_pr` unconditionally. Out-of-band CI ingest
paths don't go through this orchestrator. Today's exposure:

| Path | Triggers `process_pr_task`? | Status |
| --- | --- | --- |
| `sync_pr_task` (discovery / scheduled / webhook-driven) | Yes, unconditionally | Safe |
| GitHub webhook → `sync_ci_for_repo_shas_task(trigger_analyzer_after_sync=True)` | Yes | Safe |
| `refresh_pending_ci_for_repo` → `sync_ci_for_shas_task` (default `False`) | **No** | Stale until next sync |
| `commit_history_tasks` → `sync_ci_for_shas_task` (default `False`) | **No** | Stale until next sync |
| `analyzer/services/ci_backfill.py` → `sync_ci_for_shas_task` (default `False`) | **No** | Stale until next sync |
| `syncer/admin.py:608` ad-hoc CI-by-SHA → default `False` | **No** | Stale until next sync |
| Archive importer (doc 043) | Not by design | Stale until next sync |

For active OPEN PRs the staleness window is bounded by the next regular
`sync_pr_task` (typically ≤ 1 hour). For closed PRs and PRs not in the
active discovery cohort it is unbounded.

## Decision

Add a CI-write watermark column on `PRRevisionBuildState` and a matching
staleness predicate to the queue-window sweep + convergence canary. The
existing dirty-from-ts mechanism continues to serve revision-builder
signaling unchanged.

### Schema

New column on `analyzer.PRRevisionBuildState`:

```python
latest_ci_synced_at = models.DateTimeField(null=True, blank=True, db_index=True)
```

One row per PR (matches current `PRRevisionBuildState` granularity). Per-PR
is correct because CI freshness is a PR-level property — the same CI rows
feed every ruleset evaluated for that PR. Putting it on
`PRQueueWindowBuildState` (per-(PR, ruleset)) would create N redundant
copies and N writes per CI batch.

`db_index=True` is required because the sweep's SQL prefilter joins
against this column.

### CI-sync update

In `syncer/services/sub/ci_sync.py`, at the end of `sync_check_runs` and
`sync_status_contexts`, alongside the existing
`mark_pr_revision_dirty_if_earlier(pr, earliest_ts)` call, conditionally
advance the watermark:

```python
# After the dirty-marking call, advance the CI write watermark if we
# actually wrote anything. Pure no-op invocations (idempotent re-call of
# an unchanged payload) skip this so they don't trigger redundant
# queue-window rebuilds.
if result.created > 0 or result.updated > 0:
    _bump_latest_ci_synced_at(pr, now)
```

where `result` is the `CISyncResult` the sub-sync was already accumulating
(it tracks created/updated counts), and the helper is:

```python
from django.db.models import Q


def _bump_latest_ci_synced_at(pr: PullRequest, now: datetime) -> None:
    """Advance PRRevisionBuildState.latest_ci_synced_at to `now` if newer.

    Idempotent and monotone, even under concurrent CI sub-syncs for the
    same PR. Called once per CI sub-sync invocation that actually wrote
    anything, rather than per row, to avoid N+1 writes.

    Two design choices, both following the "no-op means no-write" lesson
    from the rebuild-churn fixes (see Invariants):

    1. **Atomic conditional UPDATE rather than read-modify-write.** A
       naive `state = get_or_create(...); if now > state.x: state.x = now;
       state.save()` has a lost-update race when two CI sub-syncs run
       for the same PR concurrently — both read the same value, then
       the later writer can clobber a higher one. The single SQL
       statement below resolves the race inside the database.
    2. **WHERE-gated, not `GREATEST`-gated.** Using
       `latest_ci_synced_at=Greatest(F('...'), Value(now))` would be
       atomic too, but it would touch the row on every call (writing a
       new MVCC version and bumping `updated_at`) even when the
       watermark doesn't actually advance. The WHERE form below skips
       the write entirely when `now <= latest_ci_synced_at`, keeping
       `updated_at` truthful and matching the precedent set by
       commits `088434e` and `78c29cc`.

    `auto_now=True` on `updated_at` only fires on `save()`, so the
    UPDATE sets it explicitly. The `__lt OR __isnull` pair handles the
    first-write case (column starts null)."""
    PRRevisionBuildState.objects.get_or_create(pull_request=pr)
    PRRevisionBuildState.objects.filter(pull_request=pr).filter(
        Q(latest_ci_synced_at__lt=now) | Q(latest_ci_synced_at__isnull=True)
    ).update(latest_ci_synced_at=now, updated_at=now)
```

The `now` parameter is the same `timezone.now()` captured at the top
of each sub-sync (`ci_sync.py:205` for `sync_check_runs`, `:294` for
`sync_status_contexts`) and threaded through `_upsert_commit_check_run`
/ `_upsert_commit_status_context` for the per-row `last_synced_at`
field. Reuse it for symmetry. Note that this is **not** the same value
as the `earliest_ts` signal timestamp passed to
`mark_pr_revision_dirty_if_earlier` on the adjacent line — that's a
GitHub-event time used to decide how far back the revision builder
must rewalk. `latest_ci_synced_at` is a wall-clock "we wrote CI as of
this time" watermark, which is what the per-row `last_synced_at` clock
gives us.

A status flip (e.g. a CheckRun row going from `status=PENDING` to
`status=COMPLETED`) counts as `updated > 0` from `update_if_changed`'s
perspective, so the watermark advances. A truly idempotent re-call where
every field of every row already matches counts as `created=0, updated=0`
and the watermark does not advance — saving the cost of a pointless
queue-window sweep tick on the affected PR.

### Sweep predicate

In `rebuild_queue_windows_sweep.py`, the SQL prefilter (around lines
162–206 today) gains:

```python
needs_rebuild |= Q(
    pull_request__revision_build_state__latest_ci_synced_at__isnull=False,
    min_ruleset_state_windows_built_at__lt=F(
        "pull_request__revision_build_state__latest_ci_synced_at"
    ),
)
```

The lookup goes through `pull_request__revision_build_state` because
`PRQueueWindowBuildState` has a FK to `PullRequest`, not a direct
relation to `PRRevisionBuildState`; the latter is reachable via the
`OneToOneField` reverse accessor on the PR. (No second clause is
needed for the "some ruleset has no `windows_built_at` yet" case —
the existing `null_ruleset_state_windows_built_at_count__gt=0`
predicate already covers it.)

In `_is_ruleset_stale_for_pr`, after the existing `gh_updated_at` check
(today line 80), add:

```python
# CI was synced after the last queue-window build for this ruleset.
if (
    state.latest_ci_synced_at
    and rs_state.windows_built_at
    and rs_state.windows_built_at < state.latest_ci_synced_at
):
    return True
```

Update the docstring's "Staleness sources" list to include the new
condition.

### Convergence canary

`analyzer/tasks/collect_convergence.py` mirrors the sweep's staleness
logic when computing the `windows_stale` metric. The block to update is
around lines 77–105 (per the staleness `or`-chain). Prefetch the new
column alongside the existing build-state read, then add the same
condition to the `stale` predicate.

No new metric column is needed — `windows_stale` already covers it. If we
want visibility into "how many PRs are stale specifically because of CI
writes," we could add `windows_stale_due_to_ci` later; defer until we have
operational evidence the rollup is being driven by this case.

### Migration

```python
# analyzer/migrations/00NN_prrbs_latest_ci_synced_at.py
operations = [
    migrations.AddField(
        model_name="prrevisionbuildstate",
        name="latest_ci_synced_at",
        field=models.DateTimeField(null=True, blank=True, db_index=True),
    ),
]
```

`AddField` with a nullable column is a metadata-only operation on
Postgres. Adding the index runs a full-table scan, but
`PRRevisionBuildState` is roughly one row per PR — small enough that this
is fast in practice. Do not run it inside a transaction (Django's
auto-detected `atomic = False` is correct for index creation on Postgres).

**No data backfill.** A null `latest_ci_synced_at` means "we don't know
when CI was last synced for this PR." The sweep's existing predicates
(`windows_built_at IS NULL`, `revision_version_built` lag, etc.) already
include such PRs. Once any CI write happens post-deploy, the column gets
set.

If we wanted to avoid even one cycle of "stale because we don't know," we
could backfill from `MAX(CommitCheckRun.last_synced_at,
CommitStatusContext.last_synced_at)` per repo, but the cost of just
letting the next sweep tick re-evaluate is minimal — recommend skipping.

## Invariants

- **The `latest_ci_synced_at` column is monotone**: writers only advance
  it forward, never reset it. Verify in tests; document in the helper
  docstring.
- **One writer**: only `_bump_latest_ci_synced_at` (called from
  `sync_check_runs` / `sync_status_contexts`) writes the column. Other
  code paths must not touch it. Tests should pin this.
- **`mark_pr_revision_dirty_if_earlier` is unchanged.** It continues to
  signal "revision builder needs to rewalk back to `signal_ts`" — the
  case it was designed for. The bail-out branches that don't help with
  queue-window staleness are no longer the queue-window-staleness
  signal; they're correct as the revision-builder signal because the
  revision builder genuinely doesn't need to re-run for late-arriving
  CI on an already-built revision window.
- **The dirty mechanism and the watermark are independent.** A CI write
  always advances the watermark; it sets `dirty_from_ts` only if the
  signal predates `built_through_ts`. The two are not redundant — they
  drive different sweeps for different reasons.
- **The watermark comparison is conservative under concurrent rebuild
  + sub-sync.** `windows_built_at` is set by
  `record_queue_window_build_states` using `now_ts` captured at sweep
  batch start (`rebuild_queue_windows_sweep.py:93`), which is at-or-
  before the CI rows the rebuild actually read. So if a CI sub-sync
  races a rebuild for the same PR — sub-sync runs after `now_ts` is
  captured but before the per-PR rebuild commits — the post-rebuild
  comparison `windows_built_at < latest_ci_synced_at` may flag the PR
  for a redundant re-rebuild on the next sweep tick, but it cannot
  mistakenly leave fresh CI un-materialized. Redundant rebuild is the
  safe failure mode; missed rebuild is what we're avoiding.
- **No-op means no-write — explicitly inheriting the lesson from
  prior rebuild-churn fixes.** Past production bugs (`73d0446` "several
  more sources of queue rebuilding churn", `088434e` "do not clear
  ci_checked_* fields on noop", `78c29cc` "only bump
  ci_checked_revision_version if no actionable SHAs", `2597f93` "use
  revision_build_state to avoid checking same PRs over and over") all
  shared one root cause: bookkeeping timestamps or version counters
  advanced on no-op operations, which then made staleness comparisons
  flag fresh state as stale, which triggered another rebuild, which
  re-bumped the bookkeeping, which… etc. The watermark mechanism here
  must respect the same invariant: a CI sub-sync that produced no
  content change (`result.created == 0 and result.updated == 0`) must
  not advance `latest_ci_synced_at`, and the bump helper itself must
  not write the row when `now <= latest_ci_synced_at`. The gate at the
  call site and the WHERE clause inside the helper are both load-
  bearing for this. Tests pin both. **Reviewers tempted to "simplify"
  by making the watermark advance unconditionally on every sub-sync
  call should re-read this invariant first** — that change would
  re-introduce the exact loop class those commits fixed, since active
  PRs receive periodic content-no-op sub-syncs from `sync_pr_task`.

## Tests

In `analyzer/tests/`:

- `_bump_latest_ci_synced_at`:
  - First call sets the column (Postgres `GREATEST(NULL, now)` returns
    `now`).
  - Second call with later `now` advances forward.
  - Second call with earlier `now` is a no-op (monotone).
  - Get-or-creates `PRRevisionBuildState` if it doesn't exist.
  - Concurrency: two interleaved calls — A reads, B reads, B writes
    larger `now`, A writes smaller `now` — leave the column at the
    larger value. (Easiest to write as two threads / two transactions
    using `transaction.atomic()` blocks; the `GREATEST`-based UPDATE
    must make this pass without a select-for-update.)
  - `updated_at` is bumped on every successful UPDATE (auto_now does
    not fire for `.update()`, so the helper sets it explicitly).
- Sub-sync integration:
  - `sync_check_runs` with new rows: watermark advances to `now`.
  - `sync_check_runs` with rows that all match existing state (created=0,
    updated=0): watermark **does not** advance.
  - `sync_check_runs` with rows where status flipped (e.g. PENDING →
    COMPLETED, updated > 0): watermark advances.
  - `sync_status_contexts`: same set of cases.
  - Empty contexts list: watermark not advanced (`created=0, updated=0`).
- `_is_ruleset_stale_for_pr`:
  - `latest_ci_synced_at > windows_built_at` → returns True.
  - `latest_ci_synced_at <= windows_built_at` → no change to existing
    behavior.
  - `latest_ci_synced_at` is null → no change to existing behavior.
- `rebuild_queue_windows_sweep`:
  - PR with stale `windows_built_at` only because of CI: picked up.
  - PR with `windows_built_at >= latest_ci_synced_at`: not picked up
    (no false positive when the windows are genuinely fresh).
  - Race scenario: CI sub-sync writes after the sweep batch's `now_ts`
    is captured but before the per-PR rebuild commits. Resulting
    `latest_ci_synced_at > windows_built_at`; the *next* sweep tick
    picks the PR up for a (redundant) rebuild. This pins the
    conservative behavior described in Invariants — i.e., we'd rather
    over-rebuild once than miss a CI write.
- Convergence canary:
  - `windows_stale` includes PRs whose only staleness source is CI.
- End-to-end: ingest a CI re-run via `sync_check_runs`; confirm the next
  `rebuild_queue_windows_sweep` tick rebuilds the PR's windows even
  though revision_version did not bump.
- **No-churn regression test (mirrors the test added in `73d0446`):**
  drive `process_pr` (or the full per-PR sync orchestration) twice in
  a row with identical CI payloads, and assert all three of:
  - `latest_ci_synced_at` does not advance on the second pass.
  - `revision_version` does not advance on the second pass.
  - The second pass's `rebuild_queue_windows_sweep` tick does **not**
    rebuild the PR's windows (count rebuild invocations or check
    `windows_built_at` is unchanged).
  This is the test the design ultimately stands or falls on — if it
  passes, the watermark cannot drive a self-feeding rebuild loop on
  active PRs; if it ever starts failing, that is the signal that the
  no-op-no-write invariant has regressed somewhere upstream (e.g., a
  new field added to `_diff_fields` with unstable equality, or a
  refactor that made the watermark advance unconditionally).

## Operational Notes

- Convergence-snapshot impact during rollout: each PR's first
  post-deploy CI write flips `latest_ci_synced_at` from null to "now,"
  which (since `windows_built_at` for that PR was set on a prior
  rebuild, i.e. earlier than now) marks the PR's rulesets stale. On a
  busy repo this means a large fraction of the active cohort flips to
  `windows_stale = True` within minutes of deploy and gets re-rebuilt
  on the next 1–2 sweep ticks. Before rollout, sanity-check that the
  sweep's per-tick batch budget can absorb this one-time spike — by
  inspecting recent peak-window sweep durations or by deploying during
  a low-traffic period. The spike clears by definition once each PR
  has been rebuilt once post-deploy.
- No rollback complexity — the column is additive and the sweep predicate
  is additive. Reverting requires removing the predicate clauses and the
  `_bump_latest_ci_synced_at` call (the migration can stay or be reverted
  separately).

## References

- `qb_site/analyzer/models/pr_revision_build_state.py` — column lives here.
- `qb_site/syncer/services/sub/ci_sync.py:188–355` — call sites for
  `_bump_latest_ci_synced_at`.
- `qb_site/analyzer/tasks/rebuild_queue_windows_sweep.py:20–205` — sweep
  predicate and per-ruleset staleness check.
- `qb_site/analyzer/tasks/collect_convergence.py` — convergence canary.
- `qb_site/analyzer/services/queueboard_snapshot.py:457, 762–765` —
  consumers of materialized vs. fresh CI data.
- `docs/design-decisions/043-archive-repo-backfill-importer.md` — the
  archive importer is one of the out-of-band CI ingest paths that
  benefits; it depends on this work landing first.

## Out of scope

- **Removing `mark_pr_revision_dirty_if_earlier`'s bail-outs.** The
  helper's current semantics are correct for revision-builder signaling.
  We're not changing it.
- **Auto-refreshing materialized `PRQueueWindow` rows from fresh CI on
  read.** That would be a much larger rearchitecture — making queue
  windows lazy / view-like rather than materialized. Out of scope; the
  watermark + sweep approach lets us keep the existing materialized
  shape with bounded staleness.
- **Per-(PR, ruleset) CI watermark.** All rulesets evaluated for a PR
  read the same CI rows; per-PR granularity is sufficient.
- **Reconsidering the `trigger_analyzer_after_sync=False` defaults on
  the various CI-by-SHA callers.** With this change in place, those
  callers' staleness gaps are bounded by the next sweep tick instead of
  the next per-PR sync. We could revisit individual call sites if any
  prove to need lower staleness, but the watermark removes the urgency.
