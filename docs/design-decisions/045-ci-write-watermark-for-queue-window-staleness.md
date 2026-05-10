# CI-Write Watermark for Queue-Window Staleness

## Context

Queue windows (`PRQueueWindow`) are materialized: `rebuild_queue_windows_for_pr`
reads `CommitCheckRun` / `CommitStatusContext` rows at build time, evaluates
the ruleset's gating, and bakes the result into `PRQueueWindow` rows with
attribution FKs. After materialization, the rows do not auto-update when
CI evolves. The materialized data feeds on/off-queue determination
(`queueboard_snapshot.py:762–765`), per-PR queue-time rollups
(`first_on_queue_ts`, `total_time_on_queue`, sort key), and per-(PR,
ruleset) freshness in `PRQueueWindowBuildState`.

Before this design, CI-only changes — a re-run flipping fail → pass, a
previously-pending context completing, a webhook delivering a new
conclusion — left the materialized windows stale. The mechanism that
should signal "queue windows might be stale" is
`mark_pr_revision_dirty_if_earlier(pr, signal_ts)`, called by the CI
sub-syncs in `syncer/services/sub/ci_sync.py`. It bails when
`signal_ts >= built_through_ts`, which is the practical case for
re-runs: the helper is a no-op, the revision builder isn't re-invoked,
`revision_version` is not bumped (`_collect_ci_first_seen` tracks the
*earliest* CI per `head_sha`, so a re-run is a later event on the same
SHA and does not change windows), and the queue-window sweep's
predicate — which checked ruleset `updated_at`, `pr.gh_updated_at`,
revision-version lag, and backfill flags — found nothing stale because
it did not check CI write times.

The bug was mostly masked in production because `sync_pr_task`
unconditionally enqueues `process_pr_task` after every successful
per-PR sync, and `process_pr` calls `rebuild_queue_windows_for_pr`
unconditionally. Out-of-band CI ingest paths skip this orchestrator:
`refresh_pending_ci_for_repo`, `commit_history_tasks`,
`analyzer/services/ci_backfill.py`, ad-hoc admin syncs, and the
archive importer (doc 043) all call `sync_ci_for_shas_task` with
`trigger_analyzer_after_sync=False`. For active OPEN PRs the staleness
window was bounded by the next regular `sync_pr_task` (typically ≤ 1
hour). For closed PRs and PRs not in the active discovery cohort it
was unbounded.

## Decision

Add a CI-write watermark column on `PRRevisionBuildState` and a
matching staleness predicate to the queue-window sweep + convergence
canary. The existing dirty-from-ts mechanism continues to serve
revision-builder signaling unchanged.

### Schema
- `PRRevisionBuildState.latest_ci_synced_at: DateTimeField(null=True, blank=True, db_index=True)`
  — wall-clock high-water mark of "we last wrote a `CommitCheckRun`
  or `CommitStatusContext` row for this PR."
- One row per PR (matches existing `PRRevisionBuildState`
  granularity). Per-PR is correct because CI freshness is a PR-level
  property: the same CI rows feed every ruleset evaluated for that PR.
  Per-(PR, ruleset) would create N redundant copies and N writes per
  CI batch.
- The index is required by the sweep's SQL prefilter, which joins
  against the column.

### Writer
- `_bump_latest_ci_synced_at(pr, now)` in
  `syncer/services/sub/ci_sync.py`. Called from `sync_check_runs` and
  `sync_status_contexts`, **only** when `created > 0 or updated > 0`
  from the sub-sync's perspective. Pure no-op invocations (idempotent
  re-call of an unchanged payload) skip the bump entirely.
- Implementation is an atomic conditional UPDATE —
  `WHERE latest_ci_synced_at IS NULL OR latest_ci_synced_at < now` —
  that produces zero row writes when `now <= existing watermark`.
  Two reasons:
  1. It resolves the lost-update race when two CI sub-syncs for the
     same PR run concurrently, without a `select_for_update`.
  2. The "no-op = no-write" gate matches the precedent set by
     `088434e` / `78c29cc` / `73d0446` and is load-bearing for
     avoiding the rebuild-loop class of bug those commits fixed.
     Active PRs receive periodic content-no-op sub-syncs from
     `sync_pr_task`; if the watermark advanced unconditionally, every
     sweep tick would rebuild every active PR.
- `now` is the same `timezone.now()` captured at the top of each
  sub-sync and threaded through the per-row `last_synced_at` field —
  distinct from the `earliest_ts` *signal* timestamp passed to
  `mark_pr_revision_dirty_if_earlier` on the adjacent line, which is a
  GitHub-event time used to decide how far back the revision builder
  must rewalk.
- A status flip (e.g. `status=PENDING` → `status=COMPLETED`) counts
  as `updated > 0` from `update_if_changed`'s perspective and
  advances the watermark. A truly idempotent re-call where every
  field of every row already matches counts as `created=0,
  updated=0` and does not.

### Sweep predicate
`rebuild_queue_windows_sweep.py` gains a matching pair:
- SQL prefilter:
  ```python
  needs_rebuild |= Q(
      revision_build_state__latest_ci_synced_at__isnull=False,
      min_ruleset_state_windows_built_at__lt=F(
          "revision_build_state__latest_ci_synced_at"
      ),
  )
  ```
  The lookup is `revision_build_state__...` (the OneToOne reverse
  accessor) because the sweep iterates `PullRequest` directly. This
  matches the existing `revision_build_state__revision_version`
  lookups in the same file.
- `_is_ruleset_stale_for_pr` exact per-(PR, ruleset) check:
  `state.latest_ci_synced_at and rs_state.windows_built_at <
  state.latest_ci_synced_at → True`.

### Convergence canary
`collect_convergence.py` mirrors the sweep predicate when computing
`windows_stale`, so the canary counts CI-only-stale `(PR, ruleset)`
pairs alongside other staleness sources. No new metric column —
`windows_stale` already covers it. The existing
`# NOTE: keep these staleness conditions in sync with
_is_ruleset_stale_for_pr` comment continues to apply.

### Removed: legacy `windows_built_*` fields
Doc 024 ("per-ruleset queue-window build state") had superseded
`PRRevisionBuildState.windows_built_revision_version` and
`PRRevisionBuildState.windows_built_at` with per-(PR, ruleset)
`PRQueueWindowBuildState.windows_built_*`, but the PR-level columns
were never removed. They had become dead weight: written only to null
on revision rebuilds, with the sole live reader being a one-time
`legacy_pr_build_state` backfill function and its management command
(`backfill_queue_window_build_states`) that ran during the doc 024
rollout.

Pre-deploy verification on production found **33,445 rows** with
non-null legacy values, all of which had corresponding
`PRQueueWindowBuildState` rows already populated — pure residue, no
data lost on drop. The cleanup removed the columns and their index,
the `backfill_queue_window_build_states_for_repo` service function,
the management command, the related tests, and references in admin /
change-form template / `plan_missing_ci.py` task summaries; and added
a "superseded fields" banner to doc 024.

`ci_checked_at` was deliberately kept despite the audit finding it's
not read in any live staleness check — it's observability for
operators diagnosing stuck PRs without joining against task logs.

## Consequences
- Out-of-band CI ingest paths (CI-by-SHA backfill, commit-history
  backfill, admin syncs, archive importer) now have bounded
  staleness — ≤ 1 sweep tick — instead of "next per-PR sync"
  (≤ 1 hour for active OPEN PRs) or unbounded (for closed / dormant
  PRs).
- The watermark is a PR-level signal feeding a per-(PR, ruleset)
  staleness check. The sweep prefilter is conservative (may include
  extra candidates); the exact per-PR filter handles false positives.
- Sweep and convergence stay in lockstep: every staleness condition
  is mirrored in both files. Reviewers must update both when adding
  conditions; the cross-reference comment in `collect_convergence.py`
  flags this for them.
- `PRRevisionBuildState` ends up smaller (10 fields vs. 12 before
  this design) once Step 0 lands. Revival of the dropped legacy
  columns isn't really available — re-adding is metadata-only on
  Postgres, but the data is gone, and per-ruleset state is the only
  source of queue-window freshness now.

## Invariants
- **`latest_ci_synced_at` is monotone.** Writers advance it forward,
  never reset it.
- **One writer.** Only `_bump_latest_ci_synced_at`, called from
  `sync_check_runs` / `sync_status_contexts`. Other code paths must
  not touch the column.
- **No-op means no-write.** A CI sub-sync that produced no content
  change (`result.created == 0 and result.updated == 0`) does not
  advance the watermark, and the helper itself does not write the
  row when `now <= latest_ci_synced_at`. Both gates are load-bearing
  for avoiding the rebuild-loop class of bug fixed in `73d0446` /
  `088434e` / `78c29cc` / `2597f93`. Reviewers tempted to "simplify"
  by making the bump unconditional, or by switching the helper to a
  `Greatest`-based always-write UPDATE, should re-read this
  invariant first — that change would re-introduce the exact loop
  class those commits fixed.
- **`mark_pr_revision_dirty_if_earlier` is unchanged.** Its
  bail-out branches are correct as a *revision-builder* signal:
  late-arriving CI on an already-built revision window does not
  require a revision rebuild. The watermark is a separate signal
  targeting queue-window materialization, so the two mechanisms are
  not redundant — they drive different sweeps for different reasons.
- **Conservative under concurrent rebuild + sub-sync race.**
  `windows_built_at` is set by `record_queue_window_build_states`
  using the sweep batch's `now_ts` (captured at batch start), which
  is at-or-before the CI rows the rebuild actually read. A sub-sync
  racing a rebuild for the same PR may flag the PR for a redundant
  re-rebuild on the next sweep tick, but cannot leave fresh CI
  un-materialized. Redundant rebuild is the safe failure mode;
  missed rebuild is what we're avoiding.

## Operational Notes
- Migrations of record: `analyzer/migrations/0027_drop_legacy_windows_built_fields.py`
  (Step 0), `analyzer/migrations/0028_add_latest_ci_synced_at.py`.
  Both ship in the same PR as separate commits so Step 0 is bisectable
  and reviewable in isolation.
- Convergence-snapshot impact during rollout: each PR's first
  post-deploy CI write flips `latest_ci_synced_at` from null to
  "now," which marks the PR's rulesets stale (since `windows_built_at`
  for that PR was set on a prior rebuild, i.e. earlier than now). On
  a busy repo a large fraction of the active cohort flips to
  `windows_stale = True` within minutes of deploy and gets re-rebuilt
  on the next 1–2 sweep ticks. Sanity-check that the sweep's per-tick
  batch budget can absorb this one-time spike — by inspecting recent
  peak-window sweep durations or by deploying during a low-traffic
  period. The spike clears once each PR has been rebuilt once
  post-deploy.
- No rollback complexity for the watermark commit — the column is
  additive and the sweep predicate is additive. Reverting is removing
  the predicate clauses and the `_bump_latest_ci_synced_at` call;
  the migration can stay or be reverted separately.
- Post-deploy spot check:
  ```sql
  -- Should grow from 0 to non-zero within minutes of deploy on
  -- active repos.
  SELECT COUNT(*) FROM analyzer_prrevisionbuildstate
  WHERE latest_ci_synced_at IS NOT NULL;
  ```
  Watch the `windows_stale` convergence metric for the expected
  one-time spike + decay.
- The load-bearing test for this design is the no-churn regression
  test in `test_rebuild_queue_windows_sweep_task.py`
  (`test_no_churn_under_identical_ci_resync`): drives
  `sync_check_runs` twice with identical payloads, with sweep ticks
  between/after, and asserts the second sync produces zero CI
  writes, the watermark stays put, and the second sweep does not
  rebuild. If this ever fails, the rebuild-loop class of bug has
  regressed for CI-watermark-driven rebuilds. Mirrors the
  process-flow test added in `73d0446`.

## Deferred Follow-ups
- **Visibility into "stale specifically because of CI."** A
  `windows_stale_due_to_ci` convergence column would let us see how
  much of `windows_stale` is driven by this signal vs. the others.
  Defer until we have operational evidence the rollup is being
  driven by CI-watermark staleness disproportionately.
- **Reconsider `trigger_analyzer_after_sync=False` defaults on the
  various CI-by-SHA callers.** With the watermark in place, those
  callers' staleness gaps are bounded by the next sweep tick. We
  could revisit individual call sites if any prove to need lower
  staleness, but the watermark removes the urgency.
- **Backfill `latest_ci_synced_at` from `MAX(CommitCheckRun.last_synced_at,
  CommitStatusContext.last_synced_at)` per repo.** Skipped at deploy
  time — the cost of letting the next sweep tick re-evaluate is
  minimal. Worth revisiting only if a future operational scenario
  needs to short-circuit the post-deploy spike.

## Alternatives Considered
- **Removing `mark_pr_revision_dirty_if_earlier`'s bail-outs.**
  Rejected: the helper's current semantics are correct for
  revision-builder signaling. Late-arriving CI on an already-built
  revision window genuinely doesn't require a revision rebuild —
  the watermark gives queue-window staleness a separate signal
  targeting the right surface.
- **Auto-refreshing materialized `PRQueueWindow` rows from fresh CI
  on read.** Rejected: a much larger rearchitecture (making queue
  windows lazy / view-like rather than materialized). The watermark
  + sweep approach lets us keep the existing materialized shape
  with bounded staleness.
- **Per-(PR, ruleset) CI watermark.** Rejected: all rulesets
  evaluated for a PR read the same CI rows; per-PR granularity is
  sufficient, and per-ruleset would create N redundant copies and N
  writes per CI batch.
- **`Greatest(F('latest_ci_synced_at'), Value(now))` in the bump
  helper.** Rejected: atomic and monotone, but writes the row on
  every call, bumping `updated_at` and creating a new MVCC version
  even when the watermark doesn't actually advance. The WHERE-gated
  form skips the write entirely on no-ops, matching the
  no-op-no-write invariant the prior rebuild-churn fixes
  established.

## References
- `qb_site/analyzer/models/pr_revision_build_state.py` —
  `latest_ci_synced_at` column.
- `qb_site/syncer/services/sub/ci_sync.py` —
  `_bump_latest_ci_synced_at` and its two call sites.
- `qb_site/analyzer/tasks/rebuild_queue_windows_sweep.py` — sweep
  predicate and per-ruleset staleness check (`_is_ruleset_stale_for_pr`).
- `qb_site/analyzer/tasks/collect_convergence.py` — convergence
  canary mirroring the staleness predicate.
- `qb_site/analyzer/services/queueboard_snapshot.py:457, 762–765` —
  consumers of materialized vs. fresh CI data.
- `docs/design-decisions/024-per-ruleset-queue-window-build-state.md`
  — origin of the per-ruleset state that supersedes the legacy
  PR-level fields removed in Step 0; carries a "superseded fields"
  banner pointing here.
- `docs/design-decisions/043-archive-repo-backfill-importer.md` —
  the archive importer is one of the out-of-band CI ingest paths
  whose staleness is bounded by this design.
