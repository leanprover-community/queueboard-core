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
    commit SHAs the live syncer can no longer fetch. Concretely, this is the SHA that
    was the PR head at the time of the archive scrape, where that SHA has since been
    force-pushed away and the live syncer didn't enqueue a CI-by-SHA fetch in time.
    The archive *cannot* recover CI for SHAs that were already orphaned before the
    scrape: those are absent from the snapshot's `commits(first: 250)` connection by
    GraphQL semantics, since the connection only enumerates commits in the PR's
    branch as of the snapshot moment.
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
     - `ci_sync.sync_check_runs` / `ci_sync.sync_status_contexts` (insert/update by
       `github_node_id`; archive-mode merge per Invariants).
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
  - `syncer.PRTimelineEvent`
- Set during archive ingest on the rows the importer created (not on rows that
  pre-existed and were merely refreshed). Never touched by the live syncer's own
  writes.
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
- **`last_synced_at` on PR creation**: today, `pull_request_sync.upsert_pull_request`
  sets `pr.last_synced_at = timezone.now()` when it creates a brand-new row
  (`pull_request_sync.py:89`). For archive-mode creates the importer must override
  this — either by passing a `skip_watermark=True` parameter through to
  `upsert_pull_request`, or by resetting `last_synced_at = None` in archive code
  immediately after `upsert_pull_request` returns `created=True`. Without the
  override, the live discovery preflight will skip the PR forever and the live
  syncer will never come back to walk timeline pagination, leaving
  `timeline_backfill_done=False` and the analyzer revision builder short-circuited
  (see "Queue-window staleness after archive ingest" below and design doc
  045).
- **`sync_schema_version` untouched**: importer must not advance
  `PullRequest.sync_schema_version`. Per design doc 044, the upgrader registry is
  the sole writer of that column; advancing it from archive code would falsely
  claim that the v=2/v=3 ingestion expansions (broader `timelineItems`, nested
  `comments(first: K)`) have been satisfied for the PR, even though the legacy
  snapshot doesn't carry that data. Leave it alone; the upgrader wave will rewalk
  the PR via the live syncer when its turn comes.
- **Newer-wins guard for PR core**: if the existing DB row's `gh_updated_at` is
  newer than the archive snapshot's `updatedAt`, do not overwrite
  state/draft/title/body/head_sha/closed_at/merged_at. The archive may still
  contribute CI/timeline rows (those have their own idempotency keys).
  Implementation choice: add an optional `if_newer_than: datetime | None`
  parameter to `pull_request_sync.upsert_pull_request` rather than wrapping the
  call in archive code. The guard is generally useful (any future "older snapshot
  source" gets it for free) and the call site is safer kept in one place.
- **`head_sha` is part of the guard**: explicitly call out — overwriting `head_sha`
  with an archive-stale value silently corrupts every analyzer artifact that uses
  it as a SHA-by-time anchor. The newer-wins guard must include `head_sha` in the
  set of fields it gates.
- **Labels are additive only**: do not detach labels that exist in the live DB but not
  in the archive snapshot. The archive is older and would silently drop labels added
  later. Only attach labels whose `LabelDef` already exists for the repo; do not create
  new label catalog entries from archive data (live syncer is the catalog source of
  truth). Implementation choice: add an `additive_only=True` parameter to
  `labels_sync.sync_pr_labels`. The function today is full-replace
  (`labels_sync.py:148–172`), and the new branch should compute
  `set(archive_labels) ∩ set(existing_label_defs) - set(currently_attached)` and
  bulk-create only that.
- **CI rows are insert-or-update by `github_node_id`**: live store wins for shared
  SHAs (its rows are presumably fresher). Archive contributes rows whose
  `github_node_id` is otherwise absent — the orphan-SHA case we care about. The
  fallback uniqueness key `(repository, head_sha, name, external_id)` in
  `ci_sync.py:107–122` is exercised when archive payloads lack node ids; tests must
  cover this path on a real archive payload.
- **Timeline events are insert-by-node-id**: `sync_timeline_events` uses
  `get_or_create(pull_request, github_node_id, defaults=fields)` (`timeline_sync.py:297–300`).
  Existing rows are *not* refreshed even if the archive has fields the live row is
  missing. This is intentional (live is presumed fresher) but worth pinning with a
  test so future changes don't silently flip the semantics.

### Queue-window staleness after archive ingest

**This subsection's substance moved to design doc 045** ("CI-Write Watermark
for Queue-Window Staleness"). Doc 045 introduces a
`PRRevisionBuildState.latest_ci_synced_at` column that the CI sub-syncs
advance on every successful invocation, plus a matching staleness predicate
in `rebuild_queue_windows_sweep` and the convergence canary. The bug it
fixes is not specific to the archive importer — every out-of-band CI ingest
path (CI-by-SHA via `refresh_pending_ci_for_repo`, `commit_history_tasks`,
`ci_backfill`, the admin tool) has the same gap today.

**Dependency**: doc 043 depends on doc 045 landing first. Once 045 is
in place:

- The archive importer goes through the standard
  `sync_check_runs` / `sync_status_contexts` (with the `archive_mode` flag
  from "CI upsert: merge-don't-overwrite for archive mode" above), and
  those sub-syncs advance `latest_ci_synced_at` automatically.
- The queue-window sweep predicate's
  `windows_built_at < latest_ci_synced_at` clause picks up the affected
  PRs on the next tick.
- No archive-specific dirty-marking helper is needed.

**One archive-specific concern remains** (independent of 045): for PRs the
importer creates that didn't exist in the live DB,
`pull_request_sync.upsert_pull_request` sets `pr.last_synced_at =
timezone.now()` (`pull_request_sync.py:89`) on the create path. The
importer's call must override this so the live discovery preflight
(`gh_updated_at > last_synced_at`) can still pick the PR up later for
timeline-page backfill. Implementation choice (already noted in
"Compatibility with the live syncer" above): `skip_watermark=True`
parameter on `upsert_pull_request`.

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

### Legacy archive payload schema deficiency

The legacy `src/queueboard/queries/pr_info.graphql` predates several fields the
current syncer models require. For the CI-row recovery use case specifically,
the gaps are:

- **`StatusContext`**: legacy fragment requests `id, context, state, targetUrl,
  description` (`pr_info.graphql:49–54` and `:194–200`). It does **not**
  request `createdAt`. But `CommitStatusContext.gh_created_at` is `NOT NULL`
  in the model (declared in migration `0030_commitcheckrun_commitstatuscontext`,
  unchanged since). A direct `_upsert_commit_status_context` call with
  `gh_created_at=None` would raise `IntegrityError: NOT NULL constraint
  failed`. **Hard blocker.**
- **`CheckRun`**: legacy fragment requests `id, name, conclusion, status,
  detailsUrl` (`pr_info.graphql:56–62` and `:201–207`). It does **not**
  request `externalId`, `startedAt`, or `completedAt`. The corresponding
  model columns are all `null=True`, so insertion succeeds, but archive rows
  carry `external_id=NULL`, `gh_started_at=NULL`, `gh_completed_at=NULL`.
  This is benign at insert time but interacts badly with the merge concern
  below (overwriting live's non-null values with archive's NULLs).

#### Resolution: synthesize `gh_created_at` from `archive_timestamp`

The archive directory layout includes `data/<N>/timestamp.txt` carrying the
scrape time for that PR. When archive-mode `_upsert_commit_status_context`
encounters a payload with no `createdAt`, set `gh_created_at = archive_timestamp`
as a documented placeholder. The placeholder is monotonic with respect to the
archive snapshot ordering — it is **not** the real CI creation time. The
provenance column `archive_imported_at` lets future code distinguish
synthesized timestamps from real ones if the analyzer ever needs to.

Implementation note: the analyzer's CI evaluation (`_latest_ci_statuses_for_fragment`
in `analyzer/services/queue_windows.py`) orders by `gh_created_at`/`gh_completed_at`.
Archive-imported StatusContexts ordered by their synthesized timestamp will sort
to "around the time of the archive scrape," which is later than the real
creation time. For the orphan-SHA recovery case (where the SHA's revision
window has long since closed), this sorting behavior is benign — there is no
conflicting live data on the same SHA whose ordering matters. Verify in
analyzer tests that NULL `gh_started_at`/`gh_completed_at` on archive CheckRuns
don't crash the evaluation path; if they do, the fix is to default to
`archive_timestamp` for those too.

Alternative considered: skipping StatusContext ingestion entirely. Defensible
if the archive's StatusContext fraction is small; verify at implementation
time by counting `... on StatusContext` vs `... on CheckRun` matches across a
sample of archive payloads. If StatusContexts are rare in mathlib4's archive
era (the project switched to CheckRun-based CI early on), skipping is
simpler.

Alternative considered: making `CommitStatusContext.gh_created_at` nullable.
Rejected — the column is genuinely useful for live data (queue-window
evaluation uses it for time-ordering) and the cost of a synthesized
placeholder for archive rows only is lower than relaxing the model invariant.

### CI upsert: merge-don't-overwrite for archive mode

A subtler bug than the StatusContext blocker. Suppose live ingested a
`CommitCheckRun` with `(node_id="X", external_id="ext1", gh_started_at=T1,
gh_completed_at=T2)`. Then the archive importer ingests the same `node_id="X"`
from a legacy payload (which lacks `externalId`/`startedAt`/`completedAt`).
`upsert_if_changed` finds the existing row by `node_id="X"`, then
`update_if_changed` sees `external_id` and the timestamps as "changed" (live's
values vs archive's NULLs) and **overwrites the row's non-null values with
NULL**. Live's authoritative data is silently downgraded.

This is the CI-row analogue of the PR-core newer-wins concern. There is no
per-row `updated_at` on CI rows to compare against, so the gate has to be
field-shape-based rather than time-based.

Implementation: add an `archive_mode: bool = False` parameter to
`_upsert_commit_check_run` and `_upsert_commit_status_context`. When True:

- Strip keys whose value is `None` from `commit_values` *before* passing to
  `update_if_changed`. The "merge" is field-by-field: archive only
  contributes values for fields the legacy payload populates.
- On the CREATE path (row did not exist), insert with archive's values
  including NULLs — that's the best information we have. The synthesized
  `gh_created_at` covers the StatusContext NOT-NULL constraint.

`sync_check_runs` and `sync_status_contexts` (the public callers) accept
the same `archive_mode` flag and pass it through.

Tests:
- Archive-mode update preserves existing non-null `external_id`/timestamps
  on a row that lives previously inserted.
- Archive-mode create on a brand-new node_id stores archive's available
  fields and synthesized timestamp.
- Live-mode update behavior unchanged (the parameter defaults to False).
- Live-mode followed by archive-mode followed by live-mode: live's second
  write restores the full field set (since live ingest provides all
  fields).

### Latent ci_sync dedup issues — out of scope

Two bugs in the existing CI upsert helpers were surfaced during pre-implementation
review and are explicitly **out of scope** for this importer:

- **NULL `github_node_id` creates duplicates**: `upsert_if_changed` with
  `{"github_node_id": None}` matches no rows under SQL NULL semantics, so
  re-ingesting a payload without a node id creates a duplicate row each
  time. The legacy `pr_info.graphql` does request `id` for both
  `StatusContext` and `CheckRun`, so this does not bite the importer.
  Mentioned here only because the importer's defensive merge-mode is
  **not** a workaround — a NULL-node-id payload would still duplicate.
- **`_upsert_commit_status_context` lacks a composite-key fallback**
  analogous to `_upsert_commit_check_run`'s `(repo, head_sha, name,
  external_id)` path. Live and archive both write graphql-keyed
  StatusContext rows (always with `rest_id IS NULL`), so the fallback
  there is rarely exercised; the importer does not write
  `rest_id`-keyed rows at all.

Both warrant separate cleanup commits. They are tracked in the broader
syncer-pipeline backlog rather than as part of doc-043 work.

### Transaction boundaries
- Wrap each `import_pr_info_payload` invocation in a single
  `transaction.atomic()` block, mirroring the live `sync_pull_request_bundle`
  posture (`pr_sync_service.py:152`). The Celery task boundary is not the same
  as a transaction boundary — a parse error or DB failure halfway through must
  not leave a half-imported PR with, say, CI rows but no PullRequest row.
- Do *not* span multiple PRs per transaction. Per-PR atomicity gives the live
  syncer the maximum opportunity to interleave on the same PR table without
  contention, and it limits blast radius if a single payload triggers a
  constraint failure.
- The CI sub-syncs' `latest_ci_synced_at` advance (per design doc 045) runs
  inside the same transaction as the sub-syncs themselves. There is a small
  race window with the queue-window sweep (the sweep could read state and
  rebuild while the importer is mid-transaction), but the sweep reads CI
  rows under its own snapshot, and a watermark advance from a transaction
  that hasn't committed is invisible to the sweep. Benign.

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

### Prerequisites

**Design doc 045 ("CI-Write Watermark for Queue-Window Staleness") must land
before Commit 3 below.** Commit 3's archive-import path goes through the
standard `sync_check_runs` / `sync_status_contexts` and relies on doc 045's
`PRRevisionBuildState.latest_ci_synced_at` advancing automatically to
trigger queue-window invalidation. Without 045, archive-imported CI rows
would sit in the DB without flowing into a queue-window recompute (the
same bug that affects the other out-of-band CI ingest paths today).

Doc 045 is small and standalone — it can be implemented and shipped
independently of this doc as a self-contained queue-window-staleness fix.
Do that first; then proceed with Commits 2–6 here.

### Commit 1 (this doc) — **landed**
- Add `docs/design-decisions/043-archive-repo-backfill-importer.md`.

### Commit 2: model + bootstrap command — **landed**
- Migration adding `syncer.ArchiveImportItem`
  (`qb_site/syncer/migrations/0048_archiveimportitem.py`). Columns and
  constraints match the model spec above; row granularity is one per
  `(archive_name, pr_number)` per the open-question decision.
- `qb_site/syncer/services/archive_bootstrap.py`:
  - `enumerate_archive_pr_entries(owner, archive, branch=…, fetcher=…)`
    factored out so tests stub at the HTTP boundary cleanly.
  - Two `git/trees` REST calls per archive (root tree → `data/` tree).
    Raises if either response is `truncated=True`.
- `qb_site/syncer/management/commands/bootstrap_archive_worklist.py`:
  - `--archive`, `--repo`, `--archive-owner`, `--branch`, `--diff-against`,
    `--limit`. `bulk_create(ignore_conflicts=True)` against the
    `(archive_name, pr_number)` unique constraint for idempotent re-run.
  - Diff mode: skips PR numbers whose other-archive row has
    `status='completed'`.
- Settings wired in `qb_site/qb_site/settings/base.py` and `.env.example`
  (`ARCHIVE_IMPORT_ENABLED`, `_BATCH_SIZE`, `_TICK_SECONDS`,
  `_RAW_BASE_URL`, `_FETCH_TIMEOUT_SECONDS`,
  `_MAX_TRANSIENT_ATTEMPTS`). The scheduler-tick consumer arrives in
  Commit 4; defaults are inert until then.
- Wired the new model into `qb_site/syncer/admin.py` (read-only) and
  added `syncer_archiveimportitem` to `scripts/backup_policy.py`
  (`BACKUP_TABLES` + `TRUNCATE_TABLES`, since this is operational
  worklist state — not durable PR/CI data).
- Tests landed:
  - `qb_site/syncer/tests/models/test_archive_import_item.py` — defaults,
    unique constraint, same-PR-different-archive coexistence.
  - `qb_site/syncer/tests/services/test_archive_bootstrap.py` — happy
    path + `truncated`/missing-`data/` failure modes + non-numeric
    sub-tree filtering.
  - `qb_site/syncer/tests/management/test_bootstrap_archive_worklist_cmd.py`
    — bad `--repo`, unknown repo, full enrollment, idempotent re-run
    does not regress mutated rows, diff mode skips
    completed-elsewhere PRs, `--limit` cap.

### Commit 3: per-item importer + provenance fields — **landed**
- Migration: nullable `archive_imported_at` on `PullRequest`, `CommitCheckRun`,
  `CommitStatusContext`, `PRTimelineEvent`.
- Targeted sub-sync edits to support archive-mode (each justified above):
  - `pull_request_sync.upsert_pull_request`: add `if_newer_than: datetime | None`
    parameter; when set and the existing row's `gh_updated_at` is later, skip
    the update of all gated fields. Also add `skip_watermark: bool = False` so
    the importer can suppress the `last_synced_at = timezone.now()` assignment
    on creation; archive code passes `True`.
  - `labels_sync.sync_pr_labels`: add `additive_only: bool = False` parameter;
    when `True`, skip the detach pass and only attach labels whose `LabelDef`
    already exists for the repo.
  - `timeline_sync.sync_timeline_events`: add `archive_mode: bool = False`
    parameter as documented above (drops legacy `PullRequestReview` items
    lacking `state`/`submittedAt`; skips dismissed-review parent synthesis).
  - `ci_sync.sync_check_runs` / `sync_status_contexts` (and the underlying
    `_upsert_commit_check_run` / `_upsert_commit_status_context`): add an
    `archive_mode: bool = False` parameter. When `True`, strip NULL values
    from `commit_values` before `update_if_changed` so archive payloads do
    not downgrade live's non-null `external_id`/timestamps. CREATE path
    inserts whatever archive has (the synthesized `gh_created_at` covers
    the StatusContext NOT-NULL constraint). See "Legacy archive payload
    schema deficiency" and "CI upsert: merge-don't-overwrite for archive
    mode" above.
- `qb_site/syncer/services/archive_import.py`:
  - `fetch_pr_info(archive_name, pr_number) -> bytes`
  - `import_pr_info_payload(repository, payload, *, archive_name, archive_timestamp)`
    — wraps the call in `transaction.atomic()`; invokes:
    - `pull_request_sync.upsert_pull_request` with `if_newer_than=...`,
      `skip_watermark=True`.
    - `labels_sync.sync_pr_labels` with `additive_only=True`.
    - `timeline_sync.sync_timeline_events` with `archive_mode=True`.
    - `ci_sync.sync_check_runs` / `sync_status_contexts` with
      `archive_mode=True` (per "CI upsert: merge-don't-overwrite for archive
      mode" above). These automatically advance
      `PRRevisionBuildState.latest_ci_synced_at` per design doc 045, which
      is what triggers the queue-window sweep to pick up the PR; no
      archive-specific dirty-marking helper is needed.
  - For created PRs, reset `pr.last_synced_at = None` after
    `upsert_pull_request` returns `created=True` (covered by the
    `skip_watermark=True` parameter on `upsert_pull_request`).
  - Stamps `archive_imported_at` on rows the call inserted (return values from
    the sub-syncs identify created vs. updated rows).
  - **Not invoked from archive code:** `_sync_inline_review_comments` (no
    nested `comments` data in archive); writes to `sync_schema_version`
    (owned exclusively by the upgrader registry).
- `qb_site/syncer/tasks/archive_import.py::import_archive_pr_item(item_id)`.
- Tests:
  - Minimal-shape fixture imports cleanly.
  - Full-shape fixture creates expected rows in PullRequest, PRTimelineEvent,
    CommitCheckRun, CommitStatusContext, with `archive_imported_at` populated
    on the created rows.
  - Legacy-shape `PullRequestReview` timeline node (no `state`) is silently
    dropped; sibling `IssueComment` / `ReviewRequestedEvent` /
    `ReviewDismissedEvent` rows still ingest.
  - Imported `ReviewDismissedEvent` row stores no synthesized parent (legacy
    payload lacks `previousReviewState`); a subsequent live-code rewalk on
    the same PR populates the synthesized parent without conflict.
  - CHECK constraints from doc 044 Chunk 4d are satisfied (no
    `IntegrityError`) on representative archive payloads.
  - Re-import of identical payload is a no-op (no duplicate rows, no stat
    changes; dirty marker not re-advanced after the first call).
  - Live PR with newer `updatedAt` than archive: PR core untouched
    (state/title/body/head_sha all preserved); CI orphan-SHA rows still inserted.
  - Imported PRs do **not** get `sync_schema_version` advanced.
  - Labels: archive snapshot with fewer labels does not detach existing live
    labels; archive label whose `LabelDef` does not exist in the repo is
    silently dropped (catalog source-of-truth is the live syncer).
  - **`latest_ci_synced_at` advancement** (per design doc 045): after
    `import_pr_info_payload`, the PR's `PRRevisionBuildState.latest_ci_synced_at`
    is set to the import time. Verify across cases:
    new-PR / existing-PR-with-earlier-CI / existing-PR-with-later-CI /
    timeline-only-delta. Doc 045 owns the helper-level tests; this spot
    just verifies the importer threads through correctly.
  - **`last_synced_at` reset on creation**: created PR has `last_synced_at IS
    NULL` after import so the live discovery preflight will pick it up.
  - **CommitStatusContext fallback key**: archive payload lacking
    `github_node_id` on a status context exercises the
    `(repo, head_sha, name, external_id)` path without conflict.
  - **`gh_created_at` synthesis for StatusContext**: archive
    `_upsert_commit_status_context` populates `gh_created_at =
    archive_timestamp` when the legacy payload lacks `createdAt`; the row
    inserts cleanly under the NOT-NULL constraint.
  - **Archive-mode merge does not downgrade live data**: live ingest writes
    a CheckRun with `external_id` / `gh_started_at` / `gh_completed_at`,
    then archive ingests the same `github_node_id` from a legacy payload
    (those three fields NULL); the existing row's non-null values are
    preserved.
  - **Archive-mode merge on archive-only rows**: brand-new `github_node_id`
    arrives via archive; row is created with archive's available fields
    (NULLs for the missing legacy fields, synthesized `gh_created_at` for
    StatusContext).
  - **HeadRefForcePushedEvent missing actor**: legacy fragment omits `actor`
    on some old payloads; importer ingests with `actor_login = ""` rather
    than crashing.
  - Failure modes: 404 → permanent; 500 → transient with retry; parse error → permanent.

#### Interleaving tests (archive vs live ordering)

These are the scenarios most likely to expose ordering bugs and are worth
explicit coverage before deployment. Each starts from a clean DB and runs
through the listed sequence.

1. **Archive only, never seen by live** — import archive payload for a PR
   absent from live. Verify PR row, CI rows, timeline events created;
   `last_synced_at IS NULL`; `latest_ci_synced_at` set on
   `PRRevisionBuildState` (per doc 045); queue-window
   `windows_built_at` left null. After a synthetic analyzer sweep, the PR has
   revisions and queue windows.
2. **Archive first, then live (live newer)** — archive imports an older PR,
   then a live `sync_pr_task` runs. Verify live wins on PR core (post-archive
   `gh_updated_at` reflects the live `updatedAt`); labels reflect the live
   set (full-replace from live); CI rows from both sources coexist (no dup by
   `github_node_id`); `archive_imported_at` is preserved on the originally
   archive-created rows; `last_synced_at` is now non-null (set by live).
3. **Live first, then archive (archive older)** — live syncs the PR to its
   current state, then archive imports an older snapshot. Verify
   newer-wins guard preserves PR core (state, head_sha, title, body all
   from live); `additive_only=True` does not detach live labels; archive's
   CI rows for an orphan SHA (one not in live's current `commits` view)
   are inserted; live-shared CI rows untouched.
4. **Live first, then archive — newer-wins on `head_sha` specifically** —
   force-push between archive snapshot time and live sync time means
   archive's `head_sha` differs from live's. Guard must keep live's
   `head_sha`. Test that overwriting it would corrupt the analyzer
   (smoke-check by asserting the value, not by running the analyzer).
5. **Force-push interleaving (the headline use case)** — synthetic PR with
   live timeline containing one HEAD_FORCE_PUSHED event (X→Y) and CI for
   Y only. Archive contributes CI for X. After import:
   - HEAD_FORCE_PUSHED event in live timeline preserved.
   - Both X and Y CI rows present, distinct.
   - Revision builder produces two PRRevision windows; X's CI attributes
     to its window, Y's to its.
   - Queue windows recompute and (with a CI-gating ruleset) reflect both
     windows correctly.
6. **Same SHA, same `github_node_id`, both sources** — live has the row;
   archive payload has the same row with possibly older state. The
   `github_node_id` upsert must not regress live (e.g. live's
   `conclusion=SUCCESS` not downgraded to archive's `conclusion=PENDING`).
7. **Same SHA, no `github_node_id` on archive side** — archive falls back
   to `(repo, head_sha, name, external_id)`; live row inserted via
   node-id has the same composite key. Verify dedup, no `IntegrityError`.
8. **Re-import is idempotent** — running `import_pr_info_payload` twice in
   succession on the same payload produces no row count delta and no flaps in
   `archive_imported_at` (timestamp set on first import only).
   `latest_ci_synced_at` advances on each call by design — that's the
   monotone-write contract from doc 045.
9. **Archive ingest of a PR whose live row has `timeline_backfill_done=False`** —
   archive shouldn't flip this to True (importer leaves it alone); live
   timeline backfill must still run when the live syncer reaches the PR.
10. **Timeline-only delta** — archive payload contributes new timeline
    rows but no new CI rows (e.g. a PR whose CI was already fully synced
    live but a force-push event was somehow missing). The CI sub-syncs
    still run (with empty contexts); doc 045's
    `_bump_latest_ci_synced_at` skips no-op calls so the watermark is
    only advanced when CI was actually written. Revision rebuild is
    triggered via the existing timeline-sync dirty path
    (`mark_pr_revision_dirty_if_earlier` on `HEAD_FORCE_PUSHED` at
    `timeline_sync.py:360`); queue-window rebuild follows once the
    revision builder bumps `revision_version`.

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

### Convergence snapshot during the drain
- `SyncerConvergenceSnapshot` does not distinguish archive-imported rows from
  live-synced rows. During the multi-day drain, expect:
  - Higher `pr_no_revisions` / `windows_stale` counts immediately post-bootstrap
    (worklist is enrolled before the analyzer has had a chance to run).
  - Both counts trend down as the importer advances and the analyzer sweeps
    catch up. The slope, not the absolute value, is the health signal.
  - `prs_below_current_sync_schema_version` may briefly *grow* if the importer
    creates brand-new PRs with `sync_schema_version=0` faster than the
    upgrader wave can rewalk them. This is expected and self-corrects once
    the worklist drains.
- The `archive_pending` / `archive_completed` / `archive_failed_permanent`
  counters added in Commit 4 are the importer's own health signal and
  should be the primary thing we watch during the drain.

### Analyzer load during the drain
- Each archive-imported PR dirty-marks itself, triggering eventual revision
  + queue-window rebuilds. At the planned 10/min import rate this adds
  ~580 PRs/hour of analyzer work on top of normal live-sync-driven load.
- Verify during the first 24 hours of the drain that:
  - `rebuild_revisions_sweep` and `rebuild_queue_windows_sweep` task
    durations stay within the envelope they show under live-sync-only
    load (compare the Celery task-result `runtime_ms` distribution
    pre- and post-`ARCHIVE_IMPORT_ENABLED=True`).
  - `windows_stale` and `pr_no_revisions` convergence counters trend
    downward across snapshot ticks (a flat line indicates the
    analyzer is not keeping up).
- If the analyzer falls behind, the lever to tune is
  `ARCHIVE_IMPORT_BATCH_SIZE` / `ARCHIVE_IMPORT_TICK_SECONDS` (slow
  the importer), not the analyzer's own concurrency.

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
- **Zombie PRs**: a PR could exist in the archive but have been deleted from
  GitHub since (spam PRs, account deletions). The importer would create a
  PullRequest row that the live syncer can never refresh. Default: accept
  these as historical record; the status command's report should surface
  `archive_imported_at IS NOT NULL AND last_synced_at IS NULL AND created_at
  < N days ago` so we can audit them. Validating against GitHub at bootstrap
  would add ~36k REST calls and is not worth the rate-budget cost.
- **High-event PRs (>250 timeline items, >250 commits)**: the legacy snapshot
  truncates both connections at `first: 250`. For a closed PR the live
  syncer also won't paginate past its own truncation point. Should the
  importer flag these explicitly so we know which PRs have known-incomplete
  archive data? A small `archive_truncated_at_first_250` boolean on
  `ArchiveImportItem` would make this auditable cheaply.

## Progress Notes
- 2026-05-06: Initial plan written. Decision to skip tarball/clone transport in favor
  of `raw.githubusercontent.com` + persisted worklist + throttled Celery beat tick,
  driven by Heroku basic-dyno memory and disk constraints. Provenance kept internal.
- 2026-05-10: Pre-implementation review identified that the proposed
  `mark_pr_dirty_for_archive_import` helper was solving a symptom of a
  wider issue affecting every out-of-band CI ingest path
  (`refresh_pending_ci_for_repo`, `commit_history_tasks`, the analyzer's
  `ci_backfill`, the admin tool — none of which trigger
  `process_pr_task` after writing CI). Spun the fix out into design doc
  045 ("CI-Write Watermark for Queue-Window Staleness"), which adds a
  `PRRevisionBuildState.latest_ci_synced_at` column updated by
  `sync_check_runs` / `sync_status_contexts` and a matching staleness
  predicate in the queue-window sweep. With doc 045 landed, the
  archive importer needs no archive-specific dirty-marking helper —
  going through the standard CI sub-syncs (with the `archive_mode`
  flag from the CI-merge-mode subsection) suffices. Doc 043 updated:
  - "Stale-marking after archive ingest" subsection rewritten as
    "Queue-window staleness after archive ingest" pointing at doc 045.
  - Commit 3 implementation list dropped the helper call; CI sub-syncs
    now suffice for queue-window invalidation.
  - Validation Plan tests reframed in terms of `latest_ci_synced_at`
    advancement rather than `dirty_from_ts` setting.
  - Interleaving tests #1, #8, #10 updated to reflect the new
    mechanism.
  - Implementation order: doc 045 lands first; doc 043's Commit 3
    depends on it.
- 2026-05-10: Pre-implementation review of CI upsert behavior surfaced
  legacy-query schema deficiencies. Doc additions:
  - "Legacy archive payload schema deficiency" subsection: the legacy
    `pr_info.graphql` does not request `createdAt` for `StatusContext`,
    nor `externalId` / `startedAt` / `completedAt` for `CheckRun`.
    `CommitStatusContext.gh_created_at` is `NOT NULL`, so direct ingest
    fails with `IntegrityError`. Resolution: synthesize `gh_created_at =
    archive_timestamp` from the per-PR `timestamp.txt`, documented as a
    placeholder.
  - "CI upsert: merge-don't-overwrite for archive mode" subsection:
    archive payloads with shared `github_node_id` would otherwise overwrite
    live's non-null `external_id` / timestamps with NULL via
    `update_if_changed`. Add `archive_mode=True` to
    `_upsert_commit_check_run` / `_upsert_commit_status_context` (and the
    public `sync_check_runs` / `sync_status_contexts`) that strips NULL
    values before update.
  - "Latent ci_sync dedup issues — out of scope" subsection: documents the
    NULL-`github_node_id` duplicate hazard and the asymmetric
    `_upsert_commit_status_context` fallback as known live-syncer
    fragility seams that the importer does not exercise. Out of scope for
    doc 043; tracked separately.
  - Commit 3 sub-sync edits list updated with the `archive_mode` flag on
    `ci_sync` helpers; Commit 3 tests list extended with merge-mode
    scenarios and `gh_created_at` synthesis.
- 2026-05-10: Pre-implementation review of stale-marking, force-push
  interleaving, and other correctness concerns. Doc additions:
  - Tightened the orphan-SHA recovery scope claim in Goals (the archive can
    only recover CI for SHAs that were the head at scrape time, not for
    SHAs already orphaned before the scrape).
  - Added a "Stale-marking after archive ingest" subsection introducing
    `mark_pr_dirty_for_archive_import`. The existing
    `mark_pr_revision_dirty_if_earlier` is a no-op when `built_through_ts`
    is null or when `signal_ts >= built_through_ts`, so archive-contributed
    CI for the orphan-SHA case can otherwise sit in the DB without ever
    flowing into a queue-window recompute.
  - Added the `last_synced_at`-on-creation pitfall (live `upsert_pull_request`
    sets `last_synced_at = now()` for new rows, which breaks the live
    discovery preflight). Resolved via a new `skip_watermark` parameter on
    `upsert_pull_request`, applied for archive-mode creates.
  - Made the newer-wins guard a parameter on `upsert_pull_request`
    (`if_newer_than: datetime | None`) rather than archive-side wrapper
    logic; explicitly added `head_sha` to the gated set.
  - Made `additive_only=True` an explicit parameter on
    `labels_sync.sync_pr_labels` rather than a behavioral aspiration.
  - Added a "Transaction boundaries" subsection (per-PR atomicity, not
    per-batch).
  - Added `PRTimelineEvent` to the `archive_imported_at` provenance set.
  - Pinned timeline upsert as insert-only (`get_or_create(defaults=…)`
    semantics) with a test to prevent silent regressions.
  - Added an "Interleaving tests" block enumerating the 10 archive/live
    ordering scenarios most likely to expose ordering bugs (force-push
    interleaving, head_sha guard, fallback-key dedup, idempotency).
  - Added a "Convergence snapshot during the drain" operational note so
    operators expect the temporary spike in stale/unbuilt counts.
  - Added open questions for zombie PRs and high-event PR truncation.
- 2026-05-10: Commit 3 landed — provenance migration
  `0049_commitcheckrun_archive_imported_at_and_more`, archive-mode params
  on the four sub-syncs, the `syncer.services.archive_import` service,
  and the `syncer.archive_import_pr_item` Celery task. Decisions and
  deviations made during implementation:
  - **Schema gap surfaced**: the legacy `pr_info.graphql` fragment for
    `HeadRefForcePushedEvent` requests only `id` and `createdAt` — no
    `beforeCommit` / `afterCommit`. Routing such an event through
    `_extract_event_fields` with both SHAs absent would trip the
    `syncer_prtl_sha_by_type_ck` CHECK constraint at INSERT. The doc
    only flagged the missing `actor` for this event; the missing SHAs
    are a hard blocker. Resolution: `_extract_event_fields` now drops
    `HeadRefForcePushedEvent` rows that lack either SHA. Fires only
    in archive paths in practice (live `pr_bundle.graphql` always
    requests them), so live behavior is unchanged. The live syncer's
    timeline backfill picks up the real event with SHAs once it
    reaches the PR.
  - **Newer-wins guard set**: the gated set in
    `pull_request_sync.upsert_pull_request` is exactly the seven
    fields the doc enumerates (state, is_draft, title, body, head_sha,
    closed_at, merged_at). `gh_updated_at` itself is left ungated —
    archive's older value flows through, then a future live sync
    moves it forward; the guard remains correct because subsequent
    archive calls compare against the live-fresh `gh_updated_at`.
  - **CI archive-mode upsert**: factored into a small
    `_archive_mode_upsert` helper rather than threading a new
    parameter through `core.utils.db.upsert_if_changed`, so the live
    code path's IntegrityError handling is preserved unchanged. The
    helper SELECTs first, branches on found/not-found, and on UPDATE
    strips NULL values from the values dict before
    `update_if_changed`. CommitStatusContext gets no composite-key
    fallback (consistent with the doc's "out of scope" note about
    `_upsert_commit_status_context` lacking the composite path).
  - **Provenance stamping strategy**: `archive_imported_at` is set
    after sub-syncs return, by filtering rows newly created in the
    transaction (`pull_request=pr` for timeline,
    `repository=repo, head_sha__in=touched_shas` for CI, plus
    `archive_imported_at IS NULL` and `created_at >= now`). Rejected
    threading "return created PKs" through the sub-sync API to keep
    sub-sync surface area minimal. The narrow filter is robust under
    the per-PR transaction boundary; concurrent live writers don't
    race on these specific (PR, sha) tuples in practice.
  - **`gh_created_at` synthesis** for legacy StatusContext entries
    (which lack `createdAt`) lives in the `archive_import` service's
    `_split_contexts` helper, NOT in `ci_sync`. This keeps
    `sync_status_contexts` ignorant of archive concerns; the caller
    simply pre-fills `createdAt` with `archive_timestamp.isoformat()`
    when it's missing.
  - **HTTP error classification** in the task: 404 → permanent (path
    genuinely absent), 5xx + network/timeout → transient (next tick
    retries up to `ARCHIVE_IMPORT_MAX_TRANSIENT_ATTEMPTS`), other 4xx
    → permanent (auth issues, malformed paths). JSON parse and
    payload-shape errors → permanent.
- 2026-05-10: Commit 2 landed — `ArchiveImportItem` model + migration
  `0048_archiveimportitem`, `bootstrap_archive_worklist` command,
  `archive_bootstrap` service helper, settings, admin, backup policy,
  and the listed tests. Decisions made during implementation:
  - Open question on row granularity resolved as **one row per
    `(archive_name, pr_number)`**; the alternative (shared row across
    archives) was rejected to keep per-archive history visible.
  - Bootstrap fetcher takes a branch ref (default `master`) rather
    than resolving the default branch from the repo metadata; both
    archive repos use `master`, and a `--branch` flag covers the
    edge case without an extra REST call.
  - Backup policy classifies the new table as truncate-on-sanitize
    (operational state), not retain — the worklist is regenerable
    from upstream and carries no durable PR data.
  - The bootstrap command reports `considered`, `present_after`,
    and per-archive totals rather than a precise insert count;
    Postgres' `bulk_create(ignore_conflicts=True)` does not return
    PKs for skipped rows, so a one-off SELECT around the insert
    would be needed to compute the exact delta. Worth revisiting if
    operators find the current readout confusing.
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
