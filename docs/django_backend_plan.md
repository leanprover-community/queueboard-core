# Queueboard Django Backend Migration Plan

## Project Configuration
- Promote a multi-app layout: convert `qb_site/settings.py` into a package, load configuration via environment variables, and register `syncer`, `analyzer`, `api`, plus a shared `core` app for common assets.
- Wire `src/` into `PYTHONPATH` (e.g., via `pyproject.toml` editable install or `sys.path` tweak) so existing package code remains importable.
- Standardize settings using `django-environ` or `pydantic-settings`; provide `.env.example` covering DB credentials, GitHub tokens, cache backend, task broker.
- Target PostgreSQL for primary storage; configure separate settings modules for local development, CI, and deployment.
- Add base logging and observability hooks (structured logging, Sentry stub, health-check endpoint).

## App Scaffolding
- Create Django apps: `syncer`, `analyzer`, `api`, and optional `core` (shared models/utilities).
- Within each app, add subpackages for `models`, `services`, `tasks`, `serializers`, `management/commands`, and `tests` to keep concerns isolated.
- Establish `apps.py` metadata, `admin.py` registration stubs, and `urls.py` where relevant (notably for `api`).
- Document module boundaries in `docs/ARCHITECTURE.md` to help future contributors navigate the project.

## Data Modeling
- **Core**: define canonical objects (repository, contributor, label, milestone) plus timestamp mixins. Include reusable enums and query helpers.
- **Syncer raw schema**: tables for pull requests, commits, reviews, timeline events, check runs, statuses, deployment markers; persist GitHub IDs for idempotency and incremental fetches.
- Add ingestion metadata tables (sync jobs, run logs, cursors) to track API pagination state.
- **Analyzer analytics schema**: materialized models for PR cycle time, review turnaround, queue backlog snapshots, author stats, and aggregate metrics (daily/weekly).
- Consider database indexes, constraints, and data retention policies to keep storage manageable.

## Service Architecture
- Port existing scraping logic into `syncer.services` with clear interfaces (e.g., `PullRequestSyncService`). Wrap GitHub API access behind clients that handle rate limits, retries, and ETag caching.
- Introduce background execution (Celery, RQ, or Django-Q) to schedule sync cycles and analytics recomputation. Provide periodic tasks for incremental and full refresh runs.
- In `analyzer.services`, implement pipelines that transform raw tables into analytics tables, with checkpoints to avoid duplicate work.
- Capture domain events (e.g., sync completed) to trigger downstream analytics tasks.
- Provide management commands for manual runs (`sync_github`, `build_analytics`, `refresh_dashboards`).

## API Layer
- Adopt Django REST Framework for serialization, viewsets, filtering, pagination, throttling.
- Namespace routes under `/api/` with versioning (`/api/v1/`). Offer endpoints for raw entities when needed and focused analytics endpoints consumed by the frontend.
- Implement composite responses for dashboard widgets (e.g., queue snapshot, reviewer load, trend summaries).
- Enable caching headers and optional Redis cache for high-traffic endpoints.
- Provide schema documentation via `drf-spectacular` or `drf-yasg`, published alongside existing docs.

## Testing and CI
- Standardize on pytest + pytest-django; configure coverage and type-checking (mypy or pyright).
- Add factory fixtures (factory-boy) for models; seed baseline data for integration tests covering sync + analytics flows.
- Create smoke tests for API endpoints and regression tests for analytics calculations.
- Update GitHub Actions workflow to run linting (ruff, mypy), tests, migrations, and to build Docker images if applicable.
- Collect sample fixtures from existing scraped data to validate migration parity.

## Data Migration and Operations
- Write import scripts to load historical JSON/CSV dumps into the new raw tables (bulk create, upsert by GitHub ID).
- Validate analytics regeneration against legacy outputs before switching production consumers.
- Plan rollback: keep legacy scraping workflow runnable until Django pipelines reach parity; set up feature flags for API clients.
- Add operational dashboards/alerts for sync failures, latency, and data freshness.
- Document deployment steps, secrets management, and infrastructure requirements (DB, cache, worker, static hosting).

## Immediate Next Steps
1. Confirm target database, background task runner, and configuration tooling.
2. Generate Django apps and baseline scaffolding (models, services, tests directories).
3. Port current scraping package into `syncer.services`, ensuring tests cover API access and data persistence.
4. Prototype analytics pipeline for a single metric to validate data flow end-to-end.
5. Stand up initial DRF endpoint returning a simple analytics summary for the existing frontend.
