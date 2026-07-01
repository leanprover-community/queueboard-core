# Archive-Repo Backfill Importer

## Context

Two legacy repos hold per-PR JSON snapshots scraped from GitHub by the
previous `src/queueboard/dashboard.py` workflow:

- `leanprover-community/queueboard-archive` — ~30k entries under `data/`.
- `leanprover-community/queueboard-archive2` — ~36k entries; recreated as a
  flat content copy of `queueboard-archive`'s final state, without git
  history (the original `.git` had grown to several GB). archive2 is
  canonical; archive is byte-identical to archive2 on the intersection.

Each entry is `data/<N>/{pr_info.json, pr_reactions.json, timestamp.txt}`.
The `pr_info.json` payload is the raw GraphQL `pullRequest` response —
same shape as `src/queueboard/queries/pr_info.graphql`, including
`commits[].statusCheckRollup.contexts`.

Within the field set the live syncer already models, the only thing
legacy snapshots can recover that GitHub itself no longer reliably
returns is **historical CI check runs and status contexts** —
particularly:

- older head SHAs on PRs later force-pushed (orphan SHAs the live syncer
  didn't enqueue via CI-by-SHA in time);
- very old commits whose check runs have aged out of GitHub's retention.

All other PR fields the syncer captures (state, labels, timeline events,
commits) are durable on GitHub. The archive cannot recover CI for SHAs
that were already orphaned *before* the scrape — those are absent from
the snapshot's `commits(first: 250)` connection by GraphQL semantics.

Hosting is Heroku basic dynos (web + a single worker+beat dyno per
`Procfile`), with ephemeral disk and ~512 MB RAM. Bulk approaches
(full-tarball download, single long-running ingest) are not viable. The
importer must run as a long-lived gradual process across many short
tasks.

## Decision

A worklist-driven gradual importer with no clones, no tarballs, and no
on-disk staging. Public per-PR HTTP fetches from
`raw.githubusercontent.com` (no GitHub REST budget) drive idempotent
inserts through the live sub-sync code paths under an `archive_mode`
flag.

### Transport

- Bootstrap enumerates per-PR directories via two `git/trees` REST calls
  per archive (root tree → `data/` tree SHA → `data/` tree). Both
  responses must be un-truncated; raise if either is `truncated=True`.
- Per-item ingest does a single HTTP GET against
  `https://raw.githubusercontent.com/leanprover-community/<archive>/master/data/<N>/pr_info.json`,
  ~30–100 KB per request, processed-then-discarded.

### `syncer.ArchiveImportItem` (worklist)

One row per `(archive_name, pr_number)` — a PR present in both archives
has two rows. Per-archive history is retained for debugging.

Columns: `repository`, `archive_name`, `pr_number`, `archive_path`,
`archive_blob_sha` (from the trees listing), `archive_timestamp` (from
`timestamp.txt`, used by `gh_created_at` synthesis below), `status` ∈
{`pending`, `in_progress`, `completed`, `failed_transient`,
`failed_permanent`, `skipped`}, `attempts`, `last_error`,
`last_attempted_at`, `completed_at`.

Constraints: unique `(archive_name, pr_number)`; index
`(status, last_attempted_at)` for the scheduler's pick query.

Bootstrap command (`bootstrap_archive_worklist`) supports `--diff-against`
to enroll only PR numbers without a `status='completed'` row from the
other archive. `bulk_create(ignore_conflicts=True)` against the unique
key makes re-runs idempotent.

### Per-item path

Celery task `syncer.archive_import_pr_item`:

1. Atomic `UPDATE … WHERE status='pending' RETURNING *` to claim the
   item; mark `in_progress` with `last_attempted_at = now()`.
2. HTTP GET `pr_info.json`. Parse JSON.
3. Hand the unwrapped `data.repository.pullRequest` to
   `syncer/services/archive_import.py::import_pr_info_payload()`. The
   whole payload runs in one `transaction.atomic()` block. It threads
   through the existing sub-syncs with archive-mode flags:
   - `pull_request_sync.upsert_pull_request(if_newer_than=archive_updated_at,
     skip_watermark=True)`
   - `labels_sync.sync_pr_labels(additive_only=True)`
   - `timeline_sync.sync_timeline_events(archive_mode=True)` — drops
     legacy `PullRequestReview` nodes lacking `state`/`submittedAt`;
     skips dismissed-review parent synthesis (legacy fragment lacks
     `previousReviewState`).
   - `ci_sync.sync_check_runs(archive_mode=True)` /
     `sync_status_contexts(archive_mode=True)` — see invariants.
4. Stamp `archive_imported_at` on rows the call inserted (the sub-syncs
   return created-vs-updated identifiers).
5. Mark `completed` / `failed_transient` / `failed_permanent` per
   error class.

The task does **not** enqueue analyzer per-item work. Queue-window
invalidation happens automatically through
`PRRevisionBuildState.latest_ci_synced_at` (design doc 045), which the
CI sub-syncs advance on every non-no-op write.

### Throttled scheduler

Beat-driven `syncer.archive_import_tick` runs every
`ARCHIVE_IMPORT_TICK_SECONDS`, no-ops when `ARCHIVE_IMPORT_ENABLED=False`,
otherwise picks up to `ARCHIVE_IMPORT_BATCH_SIZE` rows where `status IN
('pending', 'failed_transient')` ordered by `last_attempted_at NULLS
FIRST` and enqueues each as `archive_import_pr_item.delay(...)`.

### Provenance

Nullable column `archive_imported_at` on `PullRequest`, `CommitCheckRun`,
`CommitStatusContext`, `PRTimelineEvent`. Set only on rows the importer
created; never touched on rows the live syncer wrote. Internal-only —
not exposed via `/api/v1/queueboard/snapshot`.

### Settings

| Setting | Default | Purpose |
|---|---|---|
| `ARCHIVE_IMPORT_ENABLED` | `False` | Master feature flag for the scheduler tick. |
| `ARCHIVE_IMPORT_BATCH_SIZE` | `10` | Items enqueued per tick. |
| `ARCHIVE_IMPORT_TICK_SECONDS` | `60` | Beat period for the scheduler tick. |
| `ARCHIVE_IMPORT_RAW_BASE_URL` | `https://raw.githubusercontent.com` | Override for testing / mirrors. |
| `ARCHIVE_IMPORT_FETCH_TIMEOUT_SECONDS` | `30` | Per-request timeout. |
| `ARCHIVE_IMPORT_MAX_TRANSIENT_ATTEMPTS` | `5` | Before marking `failed_permanent`. |

All declared in both `qb_site/qb_site/settings/base.py` and `.env.example`
per the project rule for env-backed settings.

## Consequences

- Force-pushed PRs whose orphan SHAs have aged out of GitHub now have
  recoverable historical CI rows in the syncer tables, attributable via
  `archive_imported_at`.
- Per-item peak memory ≤ ~5 MB (one parsed `pr_info.json` plus ORM
  state). Existing worker `--max-memory-per-child=200000` is sufficient.
  Ephemeral disk is unused (no temp files).
- The drain is multi-day (~3 days at defaults for archive2's ~36k
  items); slope of the importer's own counters and of `windows_stale` is
  the health signal.
- Live syncer state is unaffected: watermarks, discovery cursors, and
  `sync_schema_version` are never advanced by archive code. The drain
  can interleave with normal sync activity safely.
- Rollback is single-flag (`ARCHIVE_IMPORT_ENABLED=False`); imported
  rows remain — they're idempotent and harmless.

## Invariants

- **Watermarks untouched.** The importer must not advance
  `RepoBackfillCursor`, `RepoDiscoveryState`, `CIShaFetchState`, or set
  `PullRequest.last_synced_at`. The `skip_watermark=True` parameter on
  `upsert_pull_request` covers the create path (which would otherwise
  set `last_synced_at = timezone.now()`); without it the live discovery
  preflight (`gh_updated_at > last_synced_at`) would suppress the PR
  forever.
- **`sync_schema_version` untouched.** Per doc 044 the upgrader registry
  is the sole writer. Advancing it from archive code would falsely claim
  the v=2/v=3 expansions have been satisfied even though the legacy
  payload doesn't carry that data.
- **Newer-wins guard on PR core.** `upsert_pull_request(if_newer_than=…)`
  gates the PR core fields when the live row is newer than the snapshot.
  `head_sha` is the motivating case: overwriting it with an archive-stale
  value silently corrupts every analyzer artifact keyed on SHA-by-time.
  - **Follow-up (gate the whole core).** The guard originally gated only a
    subset — `state`, `draft`, `title`, `body`, `head_sha`, `closed_at`,
    `merged_at` — and let "advisory" fields (`gh_updated_at`, `additions`,
    `deletions`, `changed_files_count`, ref/repo names, `author`) flow
    through, on the assumption that a later live sync would move them
    forward. That assumption fails for closed PRs, which never get
    resynced: the importer rewound those fields to the older snapshot and
    they stuck. The guard now **skips the entire core update** when the
    live row is newer — an older snapshot can never carry a more-recent
    core value. (Create path and the archive-is-newer case are unchanged.)
  - **Provenance gap.** `PullRequest.archive_imported_at` is stamped only
    on rows the importer *created*, so pre-existing live rows it *updated*
    carry no marker. The population is instead reconstructed from the
    `ArchiveImportItem` worklist (a `completed` item whose PR has
    `archive_imported_at IS NULL`) by
    `syncer.services.archive_import.archive_touched_live_prs_queryset`. The
    one-shot remediation for the pre-fix regression is
    `manage.py resync_archive_touched_prs`, which force-resyncs that set
    (`sync_pr(force=True)` bypasses the up-to-date preflight) and heals
    both the scalar regression and the label resurrection from GitHub truth.
- **Labels are additive only.** `additive_only=True` skips the detach
  pass. Archive labels whose `LabelDef` does not exist for the repo are
  dropped (live syncer is the catalog source of truth). The function is
  otherwise full-replace.
  - **Follow-up (resurrection guard).** Additive-only was originally a
    pure add pass, which had a latent bug: because the archive snapshot
    is strictly *older* than live, its `labels.nodes` still lists a label
    the live syncer legitimately detached *after* the snapshot. The add
    pass then re-created the `PRLabel` with a fresh `created_at` (the
    import time) — a label that GitHub had already removed reappeared,
    "attached" on the import date, with a live `UNLABELED` timeline event
    proving the removal. `import_pr_info_payload` now drops any archive
    label name whose latest live LABELED/UNLABELED event is `UNLABELED`
    (`_live_removed_label_names_lower`) before the additive add. Pre-fix
    data (labels resurrected before this guard existed) is repaired by
    `manage.py resync_archive_touched_prs`, which re-fetches GitHub truth
    for every importer-touched live PR (a superset of the resurrected-label
    set) and lets the live full-replace label sync drop the stale
    attachment. A timeline-only detector was considered and rejected: a
    `PRLabel` present while the latest stored LABELED/UNLABELED event is
    `UNLABELED` is indistinguishable from a still-valid label whose re-add
    event we simply have not ingested, so deleting on that signal alone
    would silently drop valid labels on closed PRs that never resync.
- **CI archive-mode merge.** `_upsert_commit_check_run` /
  `_upsert_commit_status_context` strip NULL keys from `commit_values`
  *before* `update_if_changed` when `archive_mode=True`. Without this,
  archive's missing `external_id` / `gh_started_at` / `gh_completed_at`
  (the legacy fragment doesn't request them) would silently downgrade
  live's non-null values to NULL.
- **`gh_created_at` synthesis for `CommitStatusContext`.** The legacy
  fragment doesn't request `createdAt` but the column is `NOT NULL`. When
  the payload lacks `createdAt`, archive-mode sets `gh_created_at =
  archive_timestamp` (from `data/<N>/timestamp.txt`). A documented
  placeholder, not the real CI creation time. Provenance via
  `archive_imported_at` lets future code distinguish.
- **CheckRun timestamp synthesis from `commit.committedDate`.** The
  legacy fragment doesn't request `startedAt` / `completedAt` either.
  Initially the importer accepted NULL for both — discovered mid-drain
  that the analyzer's CI evaluation silently *drops* CheckRuns with NULL
  on both timestamps. Fix: synthesize `gh_completed_at` from
  `commit.committedDate` when both are NULL, keyed on the same
  archive-payload commit context that already drives orphan-SHA
  attribution. Required re-pending of all `completed`/`in_progress`/
  `failed_transient` rows from the prior drain.
- **Timeline ingest goes through `_extract_event_fields`.** This
  satisfies the three CHECK constraints from doc 044 Chunk 4d
  automatically (by-type routing for `requested_reviewer_login` /
  `requested_team_slug`, their mutex, and by-type routing for
  `inline_comment_total_count`). Archive-specific normalization must not
  bypass it.
- **Per-PR atomic transactions.** One `transaction.atomic()` per
  `import_pr_info_payload` call; never span multiple PRs. The Celery
  task boundary is not the same as a transaction boundary — a parse
  error or constraint failure mid-PR must not leave a half-imported PR
  with CI rows but no PullRequest row.
- **No `_sync_inline_review_comments`.** The legacy
  `pr_info.graphql` carries `reviewThreads(first: 100)` with comment
  bodies but not the per-`PullRequestReview.comments(first: K)`
  connection the live ingest expects. Inline comments and
  `PRReviewInlineCommentBackfill` rows are re-fetchable for surviving
  PRs via the v=3 upgrader rewalk; the importer does not invoke that
  path.

## Operational Notes

### Order of operations

1. **Prerequisite: doc 045 must land first.** Without
   `latest_ci_synced_at` advancing, archive-imported CI rows sit in the
   DB without flowing into a queue-window recompute — the same gap that
   affects every out-of-band CI ingest path.
2. Deploy with `ARCHIVE_IMPORT_ENABLED=False`. Run
   `bootstrap_archive_worklist --archive queueboard-archive2 --repo
   leanprover-community/mathlib4`. Verify counts.
3. Set `ARCHIVE_IMPORT_ENABLED=True`. Watch the `archive_pending` /
   `archive_completed` / `archive_failed_permanent` counters on
   `SyncerConvergenceSnapshot`. Tune `ARCHIVE_IMPORT_BATCH_SIZE` if
   throughput needs adjustment.
4. When `pending == 0` and `failed_transient == 0`, the analyzer beat
   sweeps catch up over the next 1–2 ticks via doc 045's watermark.

### Status command

`python manage.py archive_import_status [--repo OWNER/NAME] [--errors N]`
prints per-archive counts (pending / in_progress / completed / failed_*)
plus oldest-pending and recent-error samples.

### Forcing analyzer catch-up

Beat sweeps `analyzer.rebuild_revisions_sweep` and
`analyzer.rebuild_queue_windows_sweep` run on
`ANALYZER_*_SWEEP_PERIOD_SECONDS` (default 600s = 10 min) with
`ANALYZER_*_SWEEP_MAX_PRS_PER_REPO=100`. After a large drain, a single
force-kick clears the backlog faster than tuning the periodic schedule:

```bash
heroku run -a <app> -- python qb_site/manage.py shell -c "
from analyzer.tasks.rebuild_revisions_sweep import rebuild_revisions_sweep_task
from analyzer.tasks.rebuild_queue_windows_sweep import rebuild_queue_windows_sweep_task
print('revisions:', rebuild_revisions_sweep_task.delay(max_prs_per_repo=500).id)
print('queue windows:', rebuild_queue_windows_sweep_task.delay(max_prs_per_repo=2000).id)
"
```

Kick revisions first; queue windows derive from revisions. If a one-shot
isn't enough, bump `ANALYZER_QUEUE_WINDOWS_SWEEP_MAX_PRS_PER_REPO`
temporarily on the dyno; cadence (`_PERIOD_SECONDS`) is rarely the
bottleneck.

### Heroku CLI gotchas

- Heroku CLI consumes `--flag` args before passing through. Wrap with
  `--`:
  `heroku run -a <app> -- python qb_site/manage.py bootstrap_archive_worklist --archive ...`
- `heroku pg:psql` does not accept passthrough psql flags like `-tA`.
  For machine-readable output, use `-c "SELECT …"` and strip table
  chrome with `awk '/^ *[0-9]+ *$/ {print $1}'`.

### Convergence snapshot during the drain

`SyncerConvergenceSnapshot` does not distinguish archive-imported rows
from live-synced rows. Expect higher `pr_no_revisions` / `windows_stale`
counts immediately post-bootstrap; both trend down as the importer
advances and the analyzer sweeps catch up. The slope is the health
signal. `prs_below_current_sync_schema_version` may briefly *grow* if
the importer creates brand-new PRs faster than the upgrader wave can
rewalk them; self-corrects once the worklist drains.

### `last_synced_at` discovery gap

The discovery query uses `pullRequests(updatedAt: {since: <recent>})`
and won't re-list a PR whose `updatedAt` is years old. Archive-only PRs
(brand-new in the live DB, `gh_updated_at` from the archive snapshot)
therefore won't be picked up by normal discovery — the archive payload
is the only data they'll get unless someone explicitly enqueues
`sync_pr_task`. Acceptable for the orphan-SHA recovery use case (the
SHAs are gone from GitHub anyway), but worth knowing.

### Rollback

Set `ARCHIVE_IMPORT_ENABLED=False`. In-flight task finishes (or marks
`failed_transient`); no further enqueues. Imported rows remain.

### Re-pend SQL

If a code fix surfaces a class of incorrectly-imported rows, the
canonical re-pend is:

```sql
UPDATE syncer_archiveimportitem
   SET status='pending', completed_at=NULL, last_attempted_at=NULL,
       last_error='', attempts=0
 WHERE status IN ('completed', 'in_progress', 'failed_transient');
```

`failed_permanent` rows are excluded — they failed for non-recoverable
reasons (404, JSON parse, payload-shape) and the re-pend won't change
their fate. Re-import is idempotent via `_archive_mode_upsert`'s
NULL-stripping (input side only) and the `archive_imported_at__isnull`
filter on provenance stamping.

## Outcomes

- **archive2 drain completed.** ~36k items, ~3 days end-to-end.
  Of those, ~128 ended `failed_permanent` from two error classes:
  `payload_shape_error` (valid JSON, no `data.repository.pullRequest`
  node) and `json_decode_error` (empty / non-JSON file). These are
  legacy scrape failures persisted verbatim into the archive — not
  recoverable.
- **Older `queueboard-archive` import skipped.** Local empirical check
  (two `git/trees` calls per archive, set-difference of PR numbers;
  follow-up byte-equality check on the 128 `failed_permanent` PRs)
  showed:
  - `queueboard-archive` is a strict subset of `queueboard-archive2`
    (every PR directory in the older archive is also in archive2; the
    file-presence difference is 0).
  - For every `failed_permanent` PR in archive2 that exists in archive,
    the `pr_info.json` content is byte-identical between the two
    archives — consistent with archive2 being a flat content copy of
    archive's final state.
  Recovery value from bootstrapping the older archive in diff mode is
  zero. The decision is recorded here rather than tracked as an open
  follow-up.

## Deferred Follow-ups

- **Explicit per-repo management commands.**
  `rebuild_revisions_sweep --repo …`, `rebuild_queue_windows_sweep
  --repo …`, `refresh_queueboard_snapshots --repo …` were referenced in
  early drafts of this doc but never landed; the actual mechanism is
  the beat tasks plus the Django-shell `.delay()` form documented
  above. Worth building if the shell incantation feels too obscure for
  routine operator use.
- **`archive_truncated_at_first_250` boolean on `ArchiveImportItem`.**
  The legacy snapshot truncates `timelineItems` and `commits` at `first:
  250`. For a closed PR the live syncer also won't paginate past its own
  truncation point, so the importer can't recover the long tail. A
  cheap boolean would make the known-incomplete set auditable.
- **Zombie-PR audit query** surfacing `archive_imported_at IS NOT NULL
  AND last_synced_at IS NULL AND created_at < N days ago` in the status
  command. Lets operators identify PRs the importer created that the
  live syncer can never refresh (deleted spam PRs, account deletions,
  etc.) without having to write SQL.
- **`archive_imported_at` exposure in the public snapshot API.**
  Internal-only today. Revisit if a UI wants a per-row provenance hint.
- **Cross-repo support.** The model carries `repository` but the
  initial implementation hard-codes `leanprover-community/mathlib4`.
  Config change, not a schema one.

## Alternatives Considered

- **Full-tarball download / single long-running ingest.** Rejected:
  ~500 MB compressed / ~1.5–2 GB extracted is incompatible with Heroku
  basic dyno ephemeral disk and a single beat tick lifecycle.
- **Making `CommitStatusContext.gh_created_at` nullable.** Rejected:
  the column is genuinely useful for live data (queue-window evaluation
  uses it for time-ordering). The cost of a synthesized placeholder for
  archive-only rows is lower than relaxing the model invariant.
- **Skipping `StatusContext` ingestion entirely** to avoid the
  NOT-NULL constraint. Defensible if the archive's StatusContext
  fraction is small; rejected because the synthesis is cheap and
  preserves data for the mathlib4-era PRs that did use status contexts.
- **Per-`ArchiveImportItem` row keyed only by `pr_number`** with
  `archive_name` deduplicated. Rejected: per-archive history is useful
  for debugging which archive contributed a given PR.
- **Auto-validating each enrolled PR against GitHub at bootstrap time**
  to filter zombies. Rejected: ~36k REST calls is not worth the
  rate-budget cost vs. handling zombies as a downstream audit.

## References

- `qb_site/syncer/models/archive_import_item.py` — worklist model.
- `qb_site/syncer/services/archive_bootstrap.py` —
  `enumerate_archive_pr_entries` (two `git/trees` calls).
- `qb_site/syncer/services/archive_import.py` —
  `import_pr_info_payload` (atomic per-PR; threads `archive_mode` flags
  through the sub-syncs).
- `qb_site/syncer/tasks/archive_import.py` — `archive_import_tick`
  and `archive_import_pr_item`.
- `qb_site/syncer/management/commands/bootstrap_archive_worklist.py` —
  `--archive`, `--repo`, `--diff-against`, `--limit`.
- `qb_site/syncer/management/commands/archive_import_status.py` —
  operator status surface.
- `qb_site/syncer/migrations/0048_archiveimportitem.py` — model.
- `qb_site/syncer/migrations/0049_commitcheckrun_archive_imported_at_and_more.py`
  — provenance columns on `PullRequest`, `CommitCheckRun`,
  `CommitStatusContext`, `PRTimelineEvent`.
- `qb_site/syncer/migrations/0050_syncerconvergencesnapshot_archive_completed_and_more.py`
  — `archive_pending` / `archive_completed` /
  `archive_failed_permanent` counters on `SyncerConvergenceSnapshot`.
- `docs/design-decisions/044-sync-schema-versioning-and-comment-review-timeline-events.md`
  — owner of `sync_schema_version` advancement; inline review comments
  out-of-scope justification.
- `docs/design-decisions/045-ci-write-watermark-for-queue-window-staleness.md`
  — prerequisite for archive-imported CI rows to flow into queue-window
  rebuilds.
