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

## Admin and Operations Notes
- Repository admin exposes sync tools for:
  - enqueueing per-PR sync,
  - enqueueing repo-level sync/discovery tasks,
  - enqueueing history backfill tasks.
- Celery task results in admin (`django_celery_results`) are used for task summaries and debugging.
- `RepoBackfillCursor` tracks createdAt history backfill only; do not overload it for unrelated discovery state machines.

## Scheduling Notes
- Beat periodically enqueues:
  - `syncer.sync_active_repos` → fans out to `syncer.sync_repo_since` (discovery/watermark),
  - `syncer.sync_pr` — per-PR ingest (enqueued by discovery or admin),
  - `syncer.backfill_repo_history_active` → `syncer.backfill_repo_history`,
  - `syncer.backfill_repo_incomplete_prs_active` → `syncer.backfill_repo_incomplete_prs`,
  - `syncer.refresh_pending_ci_for_active_repos` → `syncer.refresh_pending_ci_for_repo`,
  - `syncer.expire_stale_ci_for_active_repos` → `syncer.expire_stale_ci_for_repo` (daily; deletes phantom pending and superseded same-SHA+name CI rows),
  - `syncer.expire_old_webhook_deliveries` (daily by default; deletes GitHubWebhookDelivery rows older than SYNCER_WEBHOOK_DELIVERY_RETENTION_DAYS),
  - `syncer.sync_ci_for_shas` / `syncer.sync_ci_for_repo_shas` — CI-by-SHA ingestion,
  - `syncer.backfill_repo_engagement_active` → `syncer.backfill_repo_engagement` (optional),
  - `syncer.upgrade_schema_versions_active` → `syncer.upgrade_schema_versions` (advances `PullRequest.sync_schema_version` toward `CURRENT_SYNC_SCHEMA_VERSION`; see Sync Schema Versioning below),
  - `syncer.harvest_commit_history` / `syncer.harvest_commit_history_sweep` (optional),
  - `syncer.collect_convergence` — records syncer convergence metrics,
  - `syncer.collect_metrics` — records sync throughput/lag metrics.
- Keep task behavior idempotent and retry-safe; prefer explicit status/reason payloads in return dicts.

## Data and Service Notes
- GraphQL bundle query: `qb_site/syncer/queries/pr_bundle.graphql`.
- Sub-sync modules under `qb_site/syncer/services/sub/` should remain narrow and composable.
- Preserve boundary: `syncer` stores raw facts; analyzer owns higher-level derived queue/revision semantics.

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
