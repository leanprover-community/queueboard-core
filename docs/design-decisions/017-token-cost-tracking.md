# Token Cost Tracking for Sync Tasks

## Context
- We need consistent visibility into GitHub GraphQL token spend across syncer workloads without scraping logs.
- Celery task results already persist compact summaries in `django_celery_results.TaskResult`; metrics snapshots aggregate these rows every 15 minutes.
- Token costs appear in different shapes (`rate_events`, `discovery_cost`, `rate_limit.cost`) and were easy to drop when tasks omitted or emptied fields.

## Decision
- Standardize every GitHub-touching syncer task to emit token cost in its TaskResult payload:
  - `sync_pr` (`qb_site/syncer/tasks/sync_tasks.py`): returns `rate_events` per header/bundle/backfill/commit page; final `rate_limit` also present.
  - `sync_repo_since` (`qb_site/syncer/tasks/sync_tasks.py`): returns `rate_events` with label `repo_discovery`, `discovery_cost`, and `rate_limit`.
  - `sync_ci_for_shas` (`qb_site/syncer/tasks/sync_tasks.py`): returns `rate_events` per CI page and `rate_limit`.
  - `harvest_commit_history` (`qb_site/syncer/tasks/commit_history_tasks.py`): returns `rate_events` labeled `commit_history_page` plus final `rate_limit`.
  - `backfill_repo_history` (`qb_site/syncer/tasks/backfill_tasks.py`): returns `rate_events` labeled `prs_created_page` plus final `rate_limit`.
  - Enqueue-only or DB-only tasks do not emit token costs: `harvest_commit_history_sweep`, `backfill_repo_history_active`, `backfill_repo_incomplete_prs(_active)`, `backfill_repo_engagement(_active)`, `refresh_pending_ci_for_repo(_active)` (delegates CI fetching to `sync_ci_for_shas`), `sync_active_repos`, `collect_convergence`, `collect_metrics`.
- Parsing rules (in `collect_metrics_task`) treat empty or missing `rate_events` the same as absent and fall back to `rate_limit.cost`; non-dict results are ignored to avoid crashes.

## Consequences
- `SyncerMetricsSnapshot` now captures:
  - `pr_token_cost`: sum of `rate_events.cost` from `sync_pr` TaskResults in the window.
  - `repo_discovery_cost`: from `discovery_cost` or `rate_limit.cost` in `sync_repo_since`.
  - `token_cost_total`: sum of token cost across all TaskResults (PR + repo + other GitHub tasks) using the standardized fields.
- Admin list for Syncer metrics exposes all cost and throughput fields, making token spend trends visible without log access.
- Tests cover token aggregation, empty `rate_events`, non-dict results, and repo discovery `rate_events` to prevent regressions.

## Operational Notes
- Ensure the Celery results backend remains enabled (`CELERY_RESULT_BACKEND=django-db`) so TaskResults are persisted for aggregation.
- When adding new GitHub calls in tasks, emit a `rate_events` entry (with `label`, `cost`, `remaining`, `resetAt`) or set `discovery_cost`; avoid returning bare scalars.
- Metrics aggregation runs via `syncer.collect_metrics` (15-minute default) and writes `SyncerMetricsSnapshot`; viewable in Django admin under Syncer metrics.
