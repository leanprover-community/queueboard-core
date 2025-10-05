# Queueboard Django Backend Migration Plan

See also: docs/legacy_data_surface.md for an overview of the legacy pipeline’s data surface and flow used to inform the new models.

## Project Configuration
- Use a settings package (`qb_site/qb_site/settings/`) with `base.py`, `local.py`, `ci.py`, `production.py`; load config from environment variables and select modules via `DJANGO_SETTINGS_MODULE`.
- Register first-party apps (`core`, `syncer`, `analyzer`, `api`) alongside Django defaults; keep shared dependencies centralized in `core`.
- Inject `src/` onto `PYTHONPATH` so the legacy package continues to work during the migration, and plan to replace ad-hoc path tweaks with a proper editable install.
- Standardize settings by reading from the process environment. `.env` files are consumed by Docker Compose only; developers who bypass Compose must export the same variables manually.
- Target PostgreSQL for all environments. SQLite fallbacks are out of scope so that local, CI, and production share the same database behavior.
- Maintain Dockerfile and docker-compose setup to emulate production locally (web + Postgres containers, shared `.env`).

### Celery in Docker Compose
- Services: `redis` (broker), `worker` (Celery workers), `beat` (Celery scheduler) run alongside `web` and `db`.
- Commands:
  - Worker: `celery -A qb_site worker -l info`
  - Beat: `celery -A qb_site beat -l info`
- Environment:
  - `PYTHONPATH=/app/qb_site:/app` for worker/beat to ensure imports resolve the inner Django package consistently.
  - `DJANGO_SETTINGS_MODULE=qb_site.settings.local` for parity with the web service.
  - Broker/Result default to Redis (`CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`) and match `.env.example`.
- Orchestration: both services depend on `db` and `redis` healthchecks and use `restart: unless-stopped`.
- Note: Because the repository has an outer `qb_site/` folder and an inner `qb_site/qb_site/` package, the `PYTHONPATH` override makes `celery -A qb_site ...` resolve correctly without additional code changes.
 - Filesystem:
   - Code is mounted read-only (`.:/app:ro`) for `web`, `worker`, and `beat` to prevent container writes into the repo.
   - A named volume `appdata:/data` holds runtime artifacts (e.g., Django `STATIC_ROOT`, `MEDIA_ROOT`, and Celery beat’s schedule file).
   - Beat persists its schedule to `/data/celerybeat-schedule` inside that volume to survive container restarts and keep the repo clean.
 - Security: `worker` and `beat` drop privileges to a non-root user via Celery CLI flags (`--uid/--gid`); beat ensures `/data` is writable before dropping privileges.

## App Scaffolding
- Directory layout (`qb_site/<app>/`) separates `models`, `services`, `tasks`, `management/commands`, `serializers`, and `tests` to keep domains isolated.
- `core` owns shared domain objects and helpers; `syncer` manages ingestion; `analyzer` computes analytics; `api` exposes JSON endpoints.
- Each app ships with an `apps.py` config and placeholder packages so migrations/tests may be added incrementally.
- Extend `api/urls.py` with DRF routers once endpoints exist; project `urls.py` already delegates `/api/` traffic here.
- Document module boundaries and workflows in `docs/ARCHITECTURE.md` so new contributors understand the split.

## Data Modeling
- **Core**: define canonical objects (repository, user), milestone as needed, plus timestamp mixins and enums shared across apps. Keep curated config here (e.g., Areas, ReviewerPreferences); avoid GitHub‑owned state.
- **Syncer raw schema**: tables for pull requests, labels (definitions), PR↔label attachments, commits, reviews, timeline events, check runs, statuses, deployment markers; persist GitHub IDs for idempotency and incremental fetches.
- Add ingestion metadata tables (sync jobs, run logs, cursors) to track API pagination state.
- **Analyzer analytics schema**: materialized models for PR cycle time, review turnaround, queue backlog snapshots, author stats, and aggregate metrics (daily/weekly).
- Consider database indexes, constraints, and retention policies to keep storage manageable.

### Core Model: Repository
- Purpose: canonical identity for a GitHub repository; minimal settings.
- Fields:
  - `owner` (str), `name` (str): unique together.
  - `github_node_id` (str, unique, nullable): the global GraphQL/REST node ID (REST exposes this as `node_id` alongside the numeric `id`).
  - `default_branch` (str): repo’s default branch from GitHub (e.g., `master`/`main`).
  - `is_active` (bool, default True), `created_at`, `updated_at` (timestamps).
- Constraints: unique `(owner, name)`; unique `github_node_id` (nullable).
- Population: syncer upserts by `github_node_id` if present, falling back to `(owner, name)`; updates `default_branch` from the repo API.
- Rationale: low‑churn identity belongs in core; high‑volume GitHub‑owned entities (PRs, events, checks) live in syncer.

## Service Architecture
- Port existing scraping logic into `syncer.services` with interfaces like `PullRequestSyncService`; wrap GitHub API access behind clients that manage rate limits, retries, and ETag caching.
- Introduce background execution (Celery, RQ, or Django-Q) to schedule sync cycles and analytics recomputation, with periodic tasks for incremental and full refresh runs.
- In `analyzer.services`, implement pipelines that transform raw tables into analytics tables, with checkpoints to avoid duplicate work.
- Capture domain events (e.g., sync completed) to trigger downstream analytics tasks, and keep orchestration idempotent.
- Provide management commands for manual runs (`sync_github`, `build_analytics`, `refresh_dashboards`).

## API Layer
- Adopt Django REST Framework for serialization, viewsets, filtering, pagination, throttling.
- Namespace routes under `/api/` with versioning (`/api/v1/`); expose raw entities as needed and focused analytics endpoints consumed by the frontend.
- Implement composite responses for dashboard widgets (queue snapshot, reviewer load, trend summaries).
- Enable caching headers and optional Redis cache for high-traffic endpoints.
- Provide schema documentation via `drf-spectacular` or `drf-yasg`, published alongside existing docs.

## Testing and CI
- Standardize on pytest + pytest-django; configure coverage and type-checking (mypy or pyright).
- Add factory fixtures (factory-boy) for models; seed baseline data for integration tests covering sync + analytics flows.
- Create smoke tests for API endpoints and regression tests for analytics calculations.
- Update GitHub Actions workflow to run linting (ruff, mypy), tests, migrations, and build Docker images if applicable.
- Collect sample fixtures from existing scraped data to validate migration parity.

## Data Migration and Operations
- Write import scripts to load historical JSON/CSV dumps into the new raw tables (bulk create, upsert by GitHub ID).
- Validate analytics regeneration against legacy outputs before switching production consumers.
- Plan rollback: keep legacy scraping workflow runnable until Django pipelines reach parity; introduce feature flags for API clients.
- Add operational dashboards/alerts for sync failures, latency, and data freshness.
- Document deployment steps, secrets management, infrastructure requirements (DB, cache, worker, static hosting), and provide a runbook for on-call triage.

## Immediate Next Steps
1. Define initial `core` domain models plus shared mixins, then scaffold migrations.
    - 1.1 Models: `Repository`, `User` (GitHub identity), `Area` (curated taxonomy), `ReviewerPreference` (capacity/rotation/areas/conflicts).
    - 1.2 Shared: timestamp mixins, external ID mixin, enums (`CIStatus`, `PRStatus`) aligned with legacy code.
    - 1.3 Admin registrations and minimal factories for tests; initial indexes/constraints (e.g., unique repo+number for PRs when introduced, unique label name per repo, unique user+area).
    - 1.4 Import stubs: seed `Area` set and a loader for `reviewer-topics.json` into `ReviewerPreference` (offline script/management command).
2. Design `syncer` raw-data models and move existing scraping code into `syncer.services` with accompanying tests.
    - 2.1 Phase 1 entities: `PullRequest`, `LabelDef` (label catalog), `PRLabel` (join), `PRAssignee` (join), `PRDependency`, `Commit`, `CheckRun/StatusContext`, `TimelineEvent`, `Review`, `Comment` (reaction optional).
    - 2.2 Persist GitHub node IDs for idempotent upserts; add ingestion metadata (jobs, cursors, run logs) and pragmatic indexes on foreign keys + `created_at`.
    - 2.3 Provide a backfill importer from existing JSON to validate parity against legacy aggregates (see docs/legacy_data_surface.md shapes).
3. Prototype an analytics computation in `analyzer` to validate data flow end-to-end.
    - 3.1 Recompute `last_status_change`, `first_on_queue`, `total_queue_time` from `TimelineEvent` into `PRStatusChange`/`ReviewCycle`.
    - 3.2 Build a `QueueSnapshot` materialization and compare counts with legacy outputs.
4. Stand up the first DRF endpoint (e.g., queue snapshot) and wire the frontend to consume it.
