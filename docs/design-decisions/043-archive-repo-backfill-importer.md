# Archive-Repo Backfill Importer

## Context
- Two legacy repos hold per-PR JSON snapshots scraped from GitHub by the previous
  `src/queueboard/dashboard.py` workflow:
  - `leanprover-community/queueboard-archive` — 30,387 entries under `data/`.
  - `leanprover-community/queueboard-archive2` — 36,140 entries under `data/`.
    Recreated from the last state of `queueboard-archive` (no git history) because the
    original `.git` had grown to several GB. archive2 is treated as canonical; archive
    survives only as a fallback for PR numbers absent from archive2.
- Each per-PR entry is `data/<N>/{pr_info.json, pr_reactions.json, timestamp.txt}`. The
  `pr_info.json` is the raw GraphQL `pullRequest` response — same shape as
  `src/queueboard/queries/pr_info.graphql`, including `commits[].statusCheckRollup.contexts`.
- Within the field set the live syncer already models, the only thing legacy snapshots
  can recover that GitHub itself no longer reliably returns is **historical CI check
  runs and status contexts** — particularly:
  - older head SHAs on PRs that were later force-pushed (orphan SHAs that the syncer
    didn't enqueue via CI-by-SHA in time);
  - very old commits whose check runs have aged out of GitHub's retention.
- All other PR fields the syncer captures (state, labels, timeline events, commits) are
  durable on GitHub. If they're missing locally, the live syncer can re-fetch directly;
  the archive is not the right fix.
- Hosting: Heroku basic dynos (web + a single worker+beat dyno per `Procfile`), with
  ephemeral disk and ~512 MB RAM. Bulk approaches (full-tarball download of
  ~500 MB compressed / ~1.5–2 GB extracted, single long-running ingest job) are not
  viable. The importer must run as a long-lived gradual process across many short tasks.

## Goals / Non-Goals
- Goals:
  - Recover historical CI snapshots into `CommitCheckRun` / `CommitStatusContext` for
    commit SHAs the live syncer can no longer fetch.
  - Idempotent and resumable: safe to stop, restart, re-run; safe to interleave with the
    live syncer.
  - Compatible with the live syncer: does not advance discovery cursors, watermarks, or
    convergence state; does not downgrade newer-wins fields.
  - Observable: per-item progress and error reasons exposed via DB-backed status table
    and a small management command.
  - Memory-bounded per task: peak working set per item ≈ one `pr_info.json` payload
    (~30–100 KB) plus normal Django ORM overhead.
- Non-goals:
  - Ingesting `pr_reactions.json`, comments, review threads, or file lists (not modeled
    in syncer today). Re-fetchable from GitHub if a future feature wants them.
  - Reconstructing point-in-time PR field values (titles, bodies, etc.) — both stores
    are latest-wins.
  - Modifying analyzer logic. Analyzer reruns naturally over imported rows via the usual
    sweep tasks.

## Proposed Design

### Source / transport
- No clones, no tarballs. Two layers:
  1. One-time worklist bootstrap via the GitHub `git/trees` API, which returns the
     entire `data/` tree (~36k entries) in one un-truncated response per archive.
  2. Per-item HTTP GET to
     `https://raw.githubusercontent.com/leanprover-community/<archive>/master/data/<N>/pr_info.json`.
     Public, unauthenticated, does not consume the GitHub REST rate budget. Each
     request is ~30–100 KB and is processed-then-discarded — never staged on disk.

### New model: `syncer.ArchiveImportItem`
- Columns:
  - `id` (PK)
  - `repository` (FK → `core.Repository`) — the live repo this archive maps to
    (initially: `leanprover-community/mathlib4`).
  - `archive_name` — `"queueboard-archive"` or `"queueboard-archive2"`.
  - `pr_number` (int).
  - `archive_path` — e.g. `"data/12345/pr_info.json"`.
  - `archive_blob_sha` (str, nullable) — from the `git/trees` listing; lets us detect
    upstream content changes if we ever bootstrap a second time.
  - `archive_timestamp` (datetime, nullable) — content of `timestamp.txt`, optional;
    fetched lazily only if we need it for tie-breaking.
  - `status` — `pending | in_progress | completed | failed_transient | failed_permanent | skipped`.
  - `attempts` (int), `last_error` (text), `last_attempted_at` (datetime),
    `completed_at` (datetime).
- Constraints:
  - `unique (archive_name, pr_number)`.
  - Index on `(status, last_attempted_at)` for the scheduler's pick query.

### Worklist bootstrap
- Management command:
  `python qb_site/manage.py bootstrap_archive_worklist --archive queueboard-archive2 --repo leanprover-community/mathlib4`.
- Calls `GET https://api.github.com/repos/leanprover-community/<archive>/git/trees/<data-tree-sha>`
  (the `data/` tree's sha is reachable from the repo's root tree; resolve in one extra
  call).
- Inserts rows with `ON CONFLICT DO NOTHING` on `(archive_name, pr_number)`. Re-runnable.
- For the older `queueboard-archive`, runs in "diff mode": only enroll PR numbers that
  are not already present from archive2 (or that completed from archive2 with errors).
  archive2 is intended to supersede archive, so we expect this set to be small (the
  numerical difference is ~5,750 entries; the actual missing-from-archive2 set may be
  smaller depending on how the recreation was done — confirm during bootstrap).

### Per-item import task
- New Celery task `syncer.tasks.archive_import.import_archive_pr_item(item_id)`.
- Steps:
  1. Atomic `update ... where status='pending' returning *` to claim the item; mark
     `in_progress` with `last_attempted_at = now()`. (Avoids double-pickup if two ticks
     overlap.)
  2. HTTP GET `pr_info.json` from `raw.githubusercontent.com` with a short timeout.
  3. Parse JSON. Hand the unwrapped `data.repository.pullRequest` payload to a new
     service `qb_site/syncer/services/archive_import.py::import_pr_info_payload()`,
     which delegates to the existing idempotent sub-syncs:
     - `pull_request_sync.upsert_pull_request` (with newer-wins guard, see Invariants).
     - `labels_sync.sync_pr_labels` (additive only — see Invariants).
     - `timeline_sync.sync_timeline_events` (insert by `github_node_id`).
     - `ci_sync.sync_commit_ci_snapshot` (insert/update by `github_node_id`).
  4. Mark `completed` (or `failed_transient` / `failed_permanent` per error class).
- This task does **not** enqueue analyzer per-item work. Analyzer rebuild is a single
  bulk sweep at the end (see Implementation Plan).

### Throttled scheduler
- Beat-driven periodic task `syncer.tasks.archive_import.archive_import_tick()`:
  - Runs every `ARCHIVE_IMPORT_TICK_SECONDS` (default 60).
  - When `ARCHIVE_IMPORT_ENABLED=False`, no-ops immediately.
  - Picks up to `ARCHIVE_IMPORT_BATCH_SIZE` rows (default 10) where
    `status in ('pending', 'failed_transient')` ordered by `last_attempted_at NULLS FIRST`,
    enqueues each as `import_archive_pr_item.delay(...)`.
- Throughput at defaults: 10/minute → ~14k/day → ~2.6 days for archive2 at 36k items.
  Tunable; can raise batch size for catch-up windows.
- Worker concurrency stays at the existing `--concurrency=2 --prefetch-multiplier=1`
  from `Procfile`. Item working set per task is small.

### Settings
| Setting | Default | Description |
|---|---|---|
| `ARCHIVE_IMPORT_ENABLED` | `False` | Master feature flag for the scheduler tick. |
| `ARCHIVE_IMPORT_BATCH_SIZE` | `10` | Items enqueued per tick. |
| `ARCHIVE_IMPORT_TICK_SECONDS` | `60` | Beat period for the scheduler tick. |
| `ARCHIVE_IMPORT_RAW_BASE_URL` | `https://raw.githubusercontent.com` | Override for testing / mirrors. |
| `ARCHIVE_IMPORT_FETCH_TIMEOUT_SECONDS` | `30` | Per-request timeout. |
| `ARCHIVE_IMPORT_MAX_TRANSIENT_ATTEMPTS` | `5` | Before marking `failed_permanent`. |

All declared in both `qb_site/qb_site/settings/base.py` and `.env.example` per the
project rule for env-backed settings.

### Provenance
- New nullable column `archive_imported_at` on:
  - `syncer.PullRequest`
  - `syncer.CommitCheckRun`
  - `syncer.CommitStatusContext`
- Set during archive ingest; never touched by the live syncer's own writes.
- Internal-only: not exposed in `/api/v1/queueboard/snapshot`. Used for diagnostics
  (e.g. "which CI rows are present only because the archive importer ran?") and for
  policy decisions if we later want to gate display.

## Subtleties / Invariants

### Compatibility with the live syncer
- **Watermarks untouched**: importer must not advance `RepoBackfillCursor`,
  `RepoDiscoveryState`, `CIShaFetchState`, or set `PullRequest.last_synced_at`. If
  `last_synced_at` reflected the archive's fetch time, the live discovery preflight
  (`updatedAt > last_synced_at`) would then suppress fresh syncs. (The previous
  `PullRequest.engagement_synced_at` column was removed in design doc 044
  Chunk 6b — `last_synced_at` is now the only "have we synced this PR?" timestamp.)
- **`sync_schema_version` untouched**: importer must not advance
  `PullRequest.sync_schema_version`. Per design doc 044, the upgrader registry is
  the sole writer of that column; advancing it from archive code would falsely
  claim that the v=2/v=3 ingestion expansions (broader `timelineItems`, nested
  `comments(first: K)`) have been satisfied for the PR, even though the legacy
  snapshot doesn't carry that data. Leave it alone; the upgrader wave will rewalk
  the PR via the live syncer when its turn comes.
- **Newer-wins guard for PR core**: in `pull_request_sync`, if the existing DB row's
  `updated_at` is newer than the archive snapshot's `updatedAt`, do not overwrite
  state/draft/title/body/closed_at/merged_at. The archive may still contribute
  CI/timeline rows (those have their own idempotency keys).
- **Labels are additive only**: do not detach labels that exist in the live DB but not
  in the archive snapshot. The archive is older and would silently drop labels added
  later. Only attach labels whose `LabelDef` already exists for the repo; do not create
  new label catalog entries from archive data (live syncer is the catalog source of truth).
- **CI rows are insert-or-update by `github_node_id`**: live store wins for shared
  SHAs (its rows are presumably fresher). Archive contributes rows whose
  `github_node_id` is otherwise absent — the orphan-SHA case we care about.
- **Timeline events are insert-by-node-id**: existing rows untouched.

### Schema drift
- Legacy GraphQL responses span ~2 years; some fields may have been renamed or added
  later. The loader treats missing fields as null rather than raising. Cover with two
  fixture files in `test/`:
  - `archive_pr_info_minimal.json` — only required fields populated.
  - `archive_pr_info_full.json` — a real sample from archive2 (e.g. PR 12345).
- **Live ingestion has expanded since the archive was written.** Design doc 044
  added new `PRTimelineEvent` types (`ISSUE_COMMENTED`, `REVIEW_APPROVED`,
  `REVIEW_CHANGES_REQUESTED`, `REVIEW_COMMENTED`, `REVIEW_DISMISSED`,
  `REVIEW_REQUESTED`, `REVIEW_REQUEST_REMOVED`), three CHECK constraints on
  `PRTimelineEvent`, and the `PRReviewInlineComment` /
  `PRReviewInlineCommentBackfill` models. The legacy `pr_info.graphql` shape only
  partially covers these:
  - `IssueComment`, `ReviewRequestedEvent`, `ReviewRequestRemovedEvent`,
    `ReviewDismissedEvent` nodes are present in the legacy `timelineItems` query
    and can flow through the live `timeline_sync.py` normalizer with no
    archive-specific changes.
  - The legacy `PullRequestReview` fragment under `timelineItems` lacks `state`
    and `submittedAt`, so the normalizer cannot route the row to one of
    `REVIEW_APPROVED` / `REVIEW_CHANGES_REQUESTED` / `REVIEW_COMMENTED`.
    Archive-mode ingestion **drops** these review nodes (consistent with how
    the live ingest already drops `state=DISMISSED` review nodes — see doc
    044 §Subtleties). The PR-level `reviews(first: 100)` connection in the
    legacy payload still feeds the soft-deprecated `approvals` /
    `commenters` aggregates via `pull_request_sync`.
  - The legacy `ReviewDismissedEvent` fragment has no `previousReviewState` or
    `actor`, so dismissed-review parent synthesis (doc 044 §Subtleties) cannot
    fire. Archive-mode keeps the dismiss-event row itself but skips synthesis;
    a later upgrader-driven rewalk under live code will populate the
    synthesized parent.
  - Inline review comments (`PullRequestReview.comments` connection) are not
    in the legacy query at all — see Out of scope.

### CHECK-constraint compatibility
- The three CHECK constraints from doc 044 Chunk 4d — by-type routing for
  `requested_reviewer_login` / `requested_team_slug`, mutex between those two
  columns, and by-type routing for `inline_comment_total_count` — are
  satisfied automatically as long as archive-mode goes through the live
  `timeline_sync._extract_event_fields` path. Importer-specific normalization
  must not bypass `_extract_event_fields`.

### Failure handling
- HTTP 404 on `raw.githubusercontent.com` → `failed_permanent` (the path genuinely does
  not exist in the archive). Do not retry.
- HTTP 5xx, network/timeout error → `failed_transient`, increment `attempts`, requeued
  by next tick. Once `attempts >= ARCHIVE_IMPORT_MAX_TRANSIENT_ATTEMPTS`, mark
  `failed_permanent` so it stops re-trying.
- JSON parse error or schema validation error → `failed_permanent` with the error stored
  in `last_error` for inspection.
- Per-item DB errors are caught at the task boundary; the item is marked
  `failed_transient` and reprocessed.

### Rate / load
- `raw.githubusercontent.com` doesn't consume the GitHub REST budget but does have
  per-IP throttling. Default 10/min should stay well under any threshold.
- Bootstrap call hits the REST API exactly twice per archive (root tree, then `data/`
  tree) — negligible.

### Out of scope for this importer
- `pr_reactions.json` — not modeled in syncer; ignored.
- `processed_data/all_pr_data.json.{aa..an}` — derived aggregator output; analyzer
  recomputes equivalents.
- **Inline review comments** (`PRReviewInlineComment` /
  `PRReviewInlineCommentBackfill`, added by doc 044 Chunk 3a). The legacy
  `pr_info.graphql` carries `reviewThreads(first: 100)` with comment bodies,
  but not the per-`PullRequestReview.comments(first: K)` connection that the
  live ingest path expects. Re-fetchable from GitHub via the live syncer's
  v=3 rewalks — same justification as `pr_reactions.json`. The importer does
  not invoke `_sync_inline_review_comments` or write
  `PRReviewInlineCommentBackfill` rows.
- **Dismissed-review parent synthesis** (doc 044 §Subtleties). The legacy
  `ReviewDismissedEvent` fragment lacks `previousReviewState`, so synthesis
  has nothing to key on. The dismiss-event row itself is still imported; a
  later upgrader-driven rewalk under live code populates the synthesized
  parent if the original review still exists in GitHub's response.
- Cross-repo support — initial implementation hard-codes
  `leanprover-community/mathlib4` mapping. The model carries `repository` so this is
  a config concern, not a schema one.

## Implementation Plan

### Commit 1 (this doc)
- Add `docs/design-decisions/043-archive-repo-backfill-importer.md`.

### Commit 2: model + bootstrap command
- Migration adding `syncer.ArchiveImportItem`.
- `qb_site/syncer/management/commands/bootstrap_archive_worklist.py`:
  - Resolves the `data/` tree sha via two `git/trees` calls.
  - Inserts pending rows for each PR directory; idempotent re-run.
  - `--diff-against` flag for the older archive (only enroll numbers absent from
    completed-from-archive2 set).
- Settings wired in `base.py` and `.env.example`.
- Tests:
  - Bootstrap with stubbed tree response → expected row set.
  - Re-run is a no-op.
  - Diff mode skips numbers already completed from another archive.

### Commit 3: per-item importer + provenance fields
- Migration: nullable `archive_imported_at` on `PullRequest`, `CommitCheckRun`,
  `CommitStatusContext`.
- `qb_site/syncer/services/archive_import.py`:
  - `fetch_pr_info(archive_name, pr_number) -> bytes`
  - `import_pr_info_payload(repository, payload, *, archive_name, archive_timestamp)`
    — invokes existing sub-syncs with archive flags (newer-wins guard, additive
    labels, archive-mode timeline). Sub-syncs to invoke today
    (post-doc-044 names — verify at implementation time):
    - `pull_request_sync.upsert_pull_request` (newer-wins guard).
    - `labels_sync.sync_pr_labels` (additive only).
    - `timeline_sync.sync_timeline_events` with an `archive_mode=True` flag
      that drops legacy-shape `PullRequestReview` items lacking `state` /
      `submittedAt` and skips dismissed-review parent synthesis (legacy
      payload has no `previousReviewState`); other event types route through
      `_extract_event_fields` unchanged so CHECK constraints remain satisfied.
    - `ci_sync.sync_commit_ci_snapshot` (insert/update by `github_node_id`).
  - **Not invoked from archive code:** `_sync_inline_review_comments` (no
    nested `comments` data in archive); writes to `sync_schema_version`
    (owned exclusively by the upgrader registry).
- `qb_site/syncer/tasks/archive_import.py::import_archive_pr_item(item_id)`.
- Tests:
  - Minimal-shape fixture imports cleanly.
  - Full-shape fixture creates expected rows in PullRequest, PRTimelineEvent,
    CommitCheckRun, CommitStatusContext.
  - Legacy-shape `PullRequestReview` timeline node (no `state`) is silently
    dropped; sibling `IssueComment` / `ReviewRequestedEvent` /
    `ReviewDismissedEvent` rows still ingest.
  - Imported `ReviewDismissedEvent` row stores no synthesized parent (legacy
    payload lacks `previousReviewState`); a subsequent live-code rewalk on
    the same PR populates the synthesized parent without conflict.
  - CHECK constraints from doc 044 Chunk 4d are satisfied (no
    `IntegrityError`) on representative archive payloads.
  - Re-import of identical payload is a no-op (no duplicate rows, no stat changes).
  - Live PR with newer `updatedAt` than archive: PR core untouched; CI orphan-SHA rows
    still inserted.
  - Imported PRs do **not** get `sync_schema_version` advanced.
  - Labels: archive snapshot with fewer labels does not detach existing live labels.
  - Failure modes: 404 → permanent; 500 → transient with retry; parse error → permanent.

### Commit 4: throttled scheduler + observability
- `archive_import_tick` task; register in beat schedule (gated by
  `ARCHIVE_IMPORT_ENABLED`).
- Status command `python manage.py archive_import_status` printing a small table:
  pending / in_progress / completed / failed_transient / failed_permanent counts per
  archive, plus oldest-pending and recent-error samples.
- Optional Django admin registration for `ArchiveImportItem` for ad-hoc inspection.
- Add counters to `SyncerConvergenceSnapshot` (new fields:
  `archive_pending`, `archive_completed`, `archive_failed_permanent`).

### Commit 5: bulk analyzer rebuild
- After archive2 worklist drains:
  - `python manage.py rebuild_revisions_sweep --repo leanprover-community/mathlib4`
  - `python manage.py rebuild_queue_windows_sweep --repo leanprover-community/mathlib4`
  - `python manage.py refresh_queueboard_snapshots --repo leanprover-community/mathlib4`
- Verify queue snapshot still serves correctly; spot-check a known force-pushed PR for
  the appearance of orphan-SHA CI rows.

### Commit 6 (optional): older-archive catch-up
- Bootstrap `queueboard-archive` in diff mode against archive2 completion set.
- Same per-item path; same observability.

## Validation Plan

### Tests
- `bootstrap_archive_worklist`:
  - Stubbed `git/trees` response → correct rows + idempotent re-run.
  - Diff-mode behavior against a pre-populated archive2 set.
- `archive_import` service:
  - Minimal-shape `pr_info.json` (missing optional fields) imports without exceptions.
  - Full-shape archive2 sample (e.g. PR 12345) produces expected PullRequest /
    timeline / CI rows.
  - Re-import is a no-op.
  - Newer-wins guard preserves live PR core state.
  - Labels additive: existing live labels not detached.
  - Orphan SHA: CI rows for a force-pushed-away SHA appear post-import.
- `import_archive_pr_item`:
  - 404 → `failed_permanent`; 5xx → bounded retry then `failed_permanent`; parse error
    → `failed_permanent`; success → `completed`.
- `archive_import_tick`:
  - Honors `ARCHIVE_IMPORT_ENABLED`.
  - Picks oldest pending up to batch size.
  - Doesn't double-pick an `in_progress` item.

### Manual checks
- End-to-end against a 5–10 item slice on local Compose, with mutations enabled.
- Verify queue snapshot regenerates cleanly with imports interleaved.
- After full archive2 run, identify a known force-pushed PR and confirm orphan-SHA CI
  rows now exist; confirm `archive_imported_at` is set.

## Operational Deployment Notes

### Order of operations
1. Ship Commits 2–4. Keep `ARCHIVE_IMPORT_ENABLED=False` in production.
2. Run `bootstrap_archive_worklist --archive queueboard-archive2`. Verify counts.
3. Set `ARCHIVE_IMPORT_ENABLED=True`. Watch convergence counters; expect ~3 days at
   defaults. Tune `ARCHIVE_IMPORT_BATCH_SIZE` if throughput needs adjustment.
4. When `archive_pending == 0` and `failed_transient == 0`, run Commit 5 sweep
   (off-peak preferred — analyzer sweeps are CPU-bound).
5. Optional: bootstrap `queueboard-archive` in diff mode and let it drain.

### Heroku basic dyno guardrails
- Single `worker` dyno runs both beat and worker (per `Procfile`). Beat tick is cheap;
  worker handles per-item HTTP + DB writes.
- Per-item peak memory ≤ ~5 MB (one `pr_info.json` parsed plus ORM state). Existing
  `--max-memory-per-child=200000` already constrains worker memory.
- Ephemeral disk is not used by the importer (no temp files).

### Rollback
- Set `ARCHIVE_IMPORT_ENABLED=False`. In-flight task will finish (or fail to
  `failed_transient`); no further enqueues happen. Imported rows remain — they're
  idempotent and harmless.

## Open Questions
- Should `archive_imported_at` ever be exposed in the public snapshot API (e.g. as a
  per-CI-row provenance hint)? Default: internal only. Revisit if a UI needs it.
- For active (still-open) PRs, should the importer also fill any older labels /
  timeline events the live store may have truncated? Default: yes via idempotent
  inserts; document explicitly if we want to gate this behavior.
- Do we want a single shared `ArchiveImportItem` row per `(repo, pr_number)` with the
  archive name de-duplicated, or one row per archive (current proposal)? Current choice
  keeps the per-archive history; arguably useful for debugging.

## Progress Notes
- 2026-05-06: Initial plan written. Decision to skip tarball/clone transport in favor
  of `raw.githubusercontent.com` + persisted worklist + throttled Celery beat tick,
  driven by Heroku basic-dyno memory and disk constraints. Provenance kept internal.
- 2026-05-10: Plan updated to reflect dependencies that landed on master while
  this importer was still at Commit 1:
  - Doc 044 Chunk 6b removed `PullRequest.engagement_synced_at`. Updated
    "Watermarks untouched" to drop the reference.
  - Doc 044 added the `sync_schema_version` framework with the upgrader
    registry as sole writer. Added an explicit invariant that the importer
    must not advance `sync_schema_version`.
  - Doc 044 added new `PRTimelineEvent` types (`REVIEW_*`,
    `ISSUE_COMMENTED`, etc.), three CHECK constraints (Chunk 4d), and the
    `PRReviewInlineComment` / `PRReviewInlineCommentBackfill` models.
    Added a "Schema drift" sub-section enumerating which legacy event
    shapes flow through unchanged, which get dropped (legacy
    `PullRequestReview` timeline nodes without `state` / `submittedAt`),
    and which get partial coverage (legacy `ReviewDismissedEvent` keeps
    the dismiss row but skips parent synthesis — no `previousReviewState`
    in the legacy fragment). Added an "Out of scope" entry for inline
    review comments (legacy `pr_info.graphql` has `reviewThreads` but not
    the per-`PullRequestReview.comments` connection the new ingest path
    expects). Updated Commit 3's plan + tests to match.

## Finalization Notes
- After Commit 6 (or its decision-not-to-do), collapse this doc into a final
  decision record:
  - keep the invariants (newer-wins, additive labels, watermark-untouched);
  - keep the operational rollback note;
  - drop the chunk-by-chunk implementation list;
  - record whether `archive_imported_at` ever became a public API hint.
