# Syncer Guidelines

## Scope
- `qb_site/syncer/` owns GitHub ingestion, discovery/backfill orchestration, PR/timeline/CI persistence, and sync convergence metrics.
- Keep ingestion logic in `services/`, orchestration in `tasks/`, and CLI/admin entrypoints in `management/commands/` and admin modules.

## High-Value Commands
```bash
# Ingest one PR bundle JSON (safe dry run for fixture checks)
docker compose exec -T web python qb_site/manage.py sync_pr_from_file \
  --repo leanprover-community/mathlib4 --file pr-30723.json --dry-run

# Generate a PR bundle with gh CLI for replay/debugging
gh api graphql \
  -F query=@qb_site/syncer/queries/pr_bundle.graphql \
  -F owner='leanprover-community' -F name='mathlib4' \
  -F number=30723 -F timelineK=150 -F commitsM=15 -F inlineCommentsPerReview=20 \
  -F timelineSince='2025-10-20T00:00:00Z' \
  > pr-30723.json

# Repo-level discovery and enqueue
docker compose exec -T web python qb_site/manage.py enqueue_repo_sync \
  --repo leanprover-community/mathlib4 --since 2025-10-20T00:00:00Z --limit 50 --states OPEN

# Manual list/sync helpers
docker compose exec -T web python qb_site/manage.py list_changed_prs \
  --repo leanprover-community/mathlib4 --since 2025-10-20T00:00:00Z --states OPEN --limit 20
docker compose exec -T web python qb_site/manage.py sync_repo \
  --repo leanprover-community/mathlib4 --since 2025-10-20T00:00:00Z --limit 50

# Archive backfill importer (design doc 043): enroll the worklist for one archive repo.
docker compose exec -T web python qb_site/manage.py bootstrap_archive_worklist \
  --archive queueboard-archive2 --repo leanprover-community/mathlib4
# Older archive in diff mode (only enroll PRs not yet completed from archive2):
docker compose exec -T web python qb_site/manage.py bootstrap_archive_worklist \
  --archive queueboard-archive --repo leanprover-community/mathlib4 \
  --diff-against queueboard-archive2

# App tests
docker compose exec -T web env DJANGO_SETTINGS_MODULE=qb_site.settings.ci python qb_site/manage.py test syncer
```

## Testing Expectations
- Canonical full-repo check entrypoint is `bash scripts/repo_check_compose.sh`.
- That script starts Docker services and may be unavailable in restricted/sandboxed environments.
- If Compose cannot run:
  - run non-DB checks (`ruff`, unit tests that do not require Postgres),
  - validate task/service logic with focused tests/mocks,
  - call out missing Compose coverage explicitly.

### Checklist for new ingestion code
These rules are derived from real incidents (notably the v=2 wire-up
gap recorded in design doc 044 §Chunk 5b). They are cheap to follow up
front and expensive to recover from when skipped.

1. **Cover every call site, not just the convenient one.** A sub-sync
   that's invoked from `sync_pull_request_bundle` is also invoked
   from the forward and backward timeline-page loops in
   `sync_pull_request` (see "Timeline ingest invariants"). Write a
   test for each call site — not because they exercise different
   code in the sub-sync, but because they pin the wire-up. The
   bundle test is the easy one to write; the back-page test is the
   one that catches "we forgot to invoke this on the rewalk path"
   bugs. The wave (`UpgradeToV*.kick`) runs the back path, not the
   bundle.
2. **Assert result-dict counters.** Any new counter the ingestion
   path accumulates into the per-sync result dict gets at least one
   assertion in tests (`self.assertEqual(res["foo_created"], N)`).
   A counter that is always zero is a silent regression; the
   assertion turns it into a loud one.
3. **Record post-deploy shape checks in the design doc's Validation
   Plan.** Concrete SQL like "after the v=N wave fires,
   `SELECT COUNT(*) FROM syncer_<table> WHERE <predicate>` should be
   non-zero on each active repo." These are the canaries a future
   agent or operator runs after each deploy boundary; without them,
   silent gaps (like the v=2 inline-comments gap) get caught only by
   manual eyeballing of live data.

## Admin and Operations Notes
- Repository admin exposes sync tools for:
  - enqueueing per-PR sync,
  - enqueueing repo-level sync/discovery tasks,
  - enqueueing history backfill tasks.
- Celery task results in admin (`django_celery_results`) are used for task summaries and debugging.
- `RepoBackfillCursor` tracks createdAt history backfill only; do not overload it for unrelated discovery state machines.

## Scheduling Notes
- Beat periodically enqueues:
  - `syncer.sync_active_repos` → fans out to `syncer.sync_repo_since` (discovery/watermark).
    Coverage invariant: the watermark advances (`mark_success`) only when the scan reached
    the cutoff AND every discovered number was enqueued or already in flight. When the batch
    cap / rate budget leaves numbers `undrained`, the watermark is held and a near-term drain
    continuation is scheduled, so the same window is rescanned until the tail is covered —
    discovery never steps the watermark past a discovered-but-un-enqueued PR (closed PRs have
    a frozen `updatedAt` and would otherwise never be revisited). `undrained` is in the task
    result/log,
  - `syncer.sync_pr` — per-PR ingest (enqueued by discovery or admin),
  - `syncer.backfill_repo_history_active` → `syncer.backfill_repo_history`,
  - `syncer.backfill_repo_incomplete_prs_active` → `syncer.backfill_repo_incomplete_prs`
    (also reconciles open PRs whose stored `state`/`is_draft` scalars contradict our own
    timeline events — closed-but-open and draft-drift — re-enqueuing a full `sync_pr` whose
    preflight `state_mismatch`/`draft_mismatch` then self-heals the row; reported as
    `inconsistent_found` in the task result),
  - `syncer.refresh_pending_ci_for_active_repos` → `syncer.refresh_pending_ci_for_repo`,
  - `syncer.expire_stale_ci_for_active_repos` → `syncer.expire_stale_ci_for_repo` (daily; deletes phantom pending and superseded same-SHA+name CI rows),
  - `syncer.expire_old_webhook_deliveries` (daily by default; deletes GitHubWebhookDelivery rows older than SYNCER_WEBHOOK_DELIVERY_RETENTION_DAYS),
  - `syncer.sync_label_catalog_for_active_repos` → `syncer.sync_label_catalog` (hourly by default via SYNCER_LABEL_CATALOG_PERIOD_SECONDS; pages through `repository.labels` and reconciles `LabelDef` rows, deleting labels removed upstream — cascades to `PRLabel`),
  - `syncer.sync_ci_for_shas` / `syncer.sync_ci_for_repo_shas` — CI-by-SHA ingestion,
  - `syncer.upgrade_schema_versions_active` → `syncer.upgrade_schema_versions` (advances `PullRequest.sync_schema_version` toward `CURRENT_SYNC_SCHEMA_VERSION`; see Sync Schema Versioning below),
  - `syncer.harvest_commit_history` / `syncer.harvest_commit_history_sweep` (optional),
  - `syncer.archive_import_tick` → `syncer.archive_import_pr_item` — beat-driven worklist drain for the archive backfill importer (design doc 043). Tick runs every `ARCHIVE_IMPORT_TICK_SECONDS` (default 60s) and gates on `ARCHIVE_IMPORT_ENABLED` so operators can toggle activity without restarting beat. Status surface: `python manage.py archive_import_status [--repo OWNER/NAME] [--errors N]`.
  - `syncer.collect_convergence` — records syncer convergence metrics,
  - `syncer.collect_metrics` — records sync throughput/lag metrics.
- Keep task behavior idempotent and retry-safe; prefer explicit status/reason payloads in return dicts.

## Data and Service Notes
- GraphQL bundle query: `qb_site/syncer/queries/pr_bundle.graphql`.
- Sub-sync modules under `qb_site/syncer/services/sub/` should remain narrow and composable.
- Preserve boundary: `syncer` stores raw facts; analyzer owns higher-level derived queue/revision semantics.

## Timeline ingest invariants
The same logical "page of timeline items" is processed by **three** distinct
code paths in `services/pr_sync_service.py`:

1. `sync_pull_request_bundle` — the bundle response (the most recent
   `timelineItems(last: $timelineK)` page).
2. The forward loop in `sync_pull_request` calling
   `client.get_timeline_page` — pages newer than the bundle's window
   (rare; only fires when `max_timeline_pages > 0`).
3. The backward loop in `sync_pull_request` calling
   `client.get_timeline_page_back` — pages older than the bundle's
   window. This is the path the schema-upgrade waves
   (`UpgradeToVN.kick`) drive; treat it as the high-volume rewalk
   path, not an edge case.

**Invariant.** Any service that ingests a sub-collection nested under
a timeline item must be invoked from **all three** paths. The GraphQL
fragments in `queries/{pr_bundle,timeline_page,timeline_page_back}.graphql`
all carry the same nested fields; if only one or two paths ingest a
particular sub-collection, rewalks (which use the back path) silently
drop data that's already on the wire.

The historical example is `_sync_inline_review_comments`. It was
initially wired only into the bundle path; the v=2 wave's back-page
rewalks created the parent `REVIEW_*` `PRTimelineEvent` rows but
never persisted the nested inline comments under them. Recovery
required a second wave (v=3) and migration `0045`. See design doc
044 §Chunk 5b.

**Practical checks when extending timeline ingest.**
- `grep -n 'sync_timeline_events(' qb_site/syncer/services/pr_sync_service.py`
  must return three call sites; every call site needs to be paired
  with the same set of sub-syncs unless there is a documented reason
  not to (today the only such exception is `_apply_assignment_opt_outs`,
  which intentionally runs only on bundle + forward, never on the
  back path, because the latest opt-out signal lives in recent
  timeline).
- Every sub-sync's contract should be "given a list of timeline
  nodes, persist whatever durable rows live nested under them" — not
  "given a bundle, persist X." Bundle-scoped helpers couple to the
  wrong abstraction.
- New result-dict counters introduced by a sub-sync must be present
  in the bundle's return dict so the page loops can `+=` them
  without `KeyError`. The page loops are the canonical accumulator;
  the bundle initializes the counter to its bundle-path contribution
  and the page loops add the page-path contributions.

## Inline-Comment Models (Design Doc 044)
- `PRReviewInlineComment` (`syncer_prreviewinlinecomment`) holds one row per
  inline comment under a `PullRequestReview`. Linked by `review_node_id` (the
  durable identifier) and a nullable `parent_review_event` FK for ORM joins.
  Idempotent insert keyed on `github_node_id` (globally unique, so plain
  `unique=True`); ingestion path uses `bulk_create(ignore_conflicts=True)`.
  Threading: `thread_root_node_id` is a best-effort root of the `replyTo`
  chain, computed within the in-flight set at ingest and tightened on rewalk
  if the original target was outside the bundle.
- `PRReviewInlineCommentBackfill` (`syncer_prreviewinlinecommentbackfill`)
  tracks reviews whose `comments(first: K)` fetch returned
  `pageInfo.hasNextPage = true` — i.e. the long tail of inline comments was
  not captured. Single-table scan is the v3 recovery sweep's hot path; the
  table stays small and dedicated for that reason. Cursor / last_attempt_at
  fields are deliberately deferred to v3.

## Sync Schema Versioning
- `PullRequest.sync_schema_version` records the highest "ingestion expansion"
  that has been satisfied for a PR. The current target is
  `qb_site/syncer/services/sync_schema_upgrades.CURRENT_SYNC_SCHEMA_VERSION`.
- The upgrader registry in `services/sync_schema_upgrades.py` is the *only*
  writer of this column. `PRSyncService` does not touch it. This avoids
  prematurely stamping a PR to a higher version when the version's upgrader
  has not actually run.
- New "we want to capture X" expansions land as a new `SchemaUpgrade` entry
  registered against the next version, plus a bump of
  `CURRENT_SYNC_SCHEMA_VERSION` — no new `*_synced_at` column on
  `PullRequest`.
- See `docs/design-decisions/044-sync-schema-versioning-and-comment-review-timeline-events.md`
  for the full design.
