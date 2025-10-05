# Repository Guidelines

## Module Focus & Layout
- `qb_site/` houses the Django project; `qb_site/qb_site/settings/` exposes layered configs (`base.py`, `local.py`, `ci.py`, `production.py`) selected via `DJANGO_SETTINGS_MODULE`.
- Domain logic is split across apps: `core` for shared models/utilities, `syncer` for GitHub ingestion, `analyzer` for derived metrics, and `api` for Django REST Framework endpoints.
- Each app reserves directories for `models/`, `services/`, `tasks/`, `management/commands/`, and `tests/`; keep new modules within the appropriate app boundary to preserve separation of concerns.

## Environment & Commands
```bash
uv run python qb_site/manage.py migrate            # apply database migrations (Postgres by default)
uv run python qb_site/manage.py runserver 0:8000   # start the local Django dev server
uv run python qb_site/manage.py collectstatic      # gather static assets before production builds
uv run pytest qb_site                              # run pytest/pytest-django suite when added
bash scripts/repo_check_compose.sh                 # run compose-based repo checks inside Docker
```

Notes
- When generating migrations on the host (outside Docker), Django may print a RuntimeWarning about
  not being able to connect to Postgres. This is expected if the DB isn’t running locally and does
  not affect migration file generation.
- Copy `.env.example` to `.env` and adjust database credentials or GitHub tokens; Docker compose reads the same file.
- PostgreSQL is the only supported database; ensure local/CI environments route through the Compose Postgres service or equivalent.
- For containerized work, run `docker compose up --build` from the repo root to start web + Postgres + Redis + Celery worker/beat.

## Coding Style & Conventions
- Continue using four-space indentation and `ruff` linting; run `uv run ruff check qb_site` before opening a PR.
- Name Django models in `PascalCase`, database tables via `db_table` only when necessary, and REST endpoints under `/api/v1/...`.
- Use type hints on services and serializers; share cross-app helpers through `core.utils` instead of duplicating code.
- Prefer explicit settings overrides through environment variables; avoid hardcoding secrets in modules or migrations.

## Testing Strategy
- Adopt `pytest` with `pytest-django`; store fixtures under `qb_site/<app>/tests/fixtures/` and factories via `factory_boy` once models land.
- Cover management commands with integration tests (`pytest qb_site/<app>/tests/test_commands.py`) to mimic cron usage.
- When touching sync flows, record sample GitHub payloads in `test/` and describe replay steps in your PR.
- Validate API responses with DRF test clients or `pytest` snapshot tools; ensure pagination, filtering, and ordering rules are asserted.

## Operational Notes
- Containers & volumes:
  - Code is bind-mounted read-only as `.:/app:ro`.
  - Runtime artifacts (Django `STATIC_ROOT`, `MEDIA_ROOT`, Celery beat schedule) write under `/data` backed by the `appdata` named volume.
  - Generate migrations on the host (not inside containers) and commit them.
- Celery:
  - Worker: `celery -A qb_site worker -l info` (non-root via `--uid/--gid`), `PYTHONPATH=/app/qb_site:/app`.
  - Beat: `celery -A qb_site beat -l info --schedule /data/celerybeat-schedule` (same env and non-root flags).
- Align new models with the migration plan in `docs/django_backend_plan.md`; coordinate raw vs. analytics tables with the future Postgres schema.
- Add observability hooks (structured logging, metrics) through Django settings rather than ad-hoc prints.
- See scheduler rationale in `docs/design-decisions/002-beat-scheduler-choice.md`.
