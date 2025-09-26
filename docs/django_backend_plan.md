# Queueboard Django Backend Migration Plan

## Project Configuration
- Use a settings package (`qb_site/settings/`) with `base.py`, `local.py`, `ci.py`, `production.py`; load config from environment variables and select modules via `DJANGO_SETTINGS_MODULE`.
- Register first-party apps (`core`, `syncer`, `analyzer`, `api`) alongside Django defaults; keep shared dependencies centralized in `core`.
- Inject `src/` onto `PYTHONPATH` so the legacy package continues to work during the migration, and plan to replace ad-hoc path tweaks with a proper editable install.
- Standardize settings by reading from the process environment. `.env` files are consumed by Docker Compose only; developers who bypass Compose must export the same variables manually.
- Target PostgreSQL for all environments. SQLite fallbacks are out of scope so that local, CI, and production share the same database behavior.
- Maintain Dockerfile and docker-compose setup to emulate production locally (web + Postgres containers, shared `.env`).

## App Scaffolding
- Directory layout (`qb_site/<app>/`) separates `models`, `services`, `tasks`, `management/commands`, `serializers`, and `tests` to keep domains isolated.
- `core` owns shared domain objects and helpers; `syncer` manages ingestion; `analyzer` computes analytics; `api` exposes JSON endpoints.
- Each app ships with an `apps.py` config and placeholder packages so migrations/tests may be added incrementally.
- Extend `api/urls.py` with DRF routers once endpoints exist; project `urls.py` already delegates `/api/` traffic here.
- Document module boundaries and workflows in `docs/ARCHITECTURE.md` so new contributors understand the split.

## Data Modeling
- **Core**: define canonical objects (repository, contributor, label, milestone) plus timestamp mixins and enums shared across apps.
- **Syncer raw schema**: tables for pull requests, commits, reviews, timeline events, check runs, statuses, deployment markers; persist GitHub IDs for idempotency and incremental fetches.
- Add ingestion metadata tables (sync jobs, run logs, cursors) to track API pagination state.
- **Analyzer analytics schema**: materialized models for PR cycle time, review turnaround, queue backlog snapshots, author stats, and aggregate metrics (daily/weekly).
- Consider database indexes, constraints, and retention policies to keep storage manageable.

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
1. Finalize environment tooling (docs now reflect Docker-compose-only `.env` usage and Postgres-only policy; background task runner decision still pending).
2. Define initial `core` domain models plus shared mixins, then scaffold migrations.
3. Design `syncer` raw-data models and move existing scraping code into `syncer.services` with accompanying tests.
4. Prototype an analytics computation in `analyzer` to validate data flow end-to-end.
5. Stand up the first DRF endpoint (e.g., queue snapshot) and wire the frontend to consume it.
