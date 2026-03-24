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
  -F number=30723 -F timelineK=150 -F commitsM=15 -F timelineSince='2025-10-20T00:00:00Z' \
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
  - `syncer.harvest_commit_history` / `syncer.harvest_commit_history_sweep` (optional),
  - `syncer.collect_convergence` — records syncer convergence metrics,
  - `syncer.collect_metrics` — records sync throughput/lag metrics.
- Keep task behavior idempotent and retry-safe; prefer explicit status/reason payloads in return dicts.

## Data and Service Notes
- GraphQL bundle query: `qb_site/syncer/queries/pr_bundle.graphql`.
- Sub-sync modules under `qb_site/syncer/services/sub/` should remain narrow and composable.
- Preserve boundary: `syncer` stores raw facts; analyzer owns higher-level derived queue/revision semantics.
