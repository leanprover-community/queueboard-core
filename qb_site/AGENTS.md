# Repository Guidelines

## Module Focus & Layout
- `qb_site/` is the Django project; settings are layered in `qb_site/qb_site/settings/{base,local,ci,production}.py`.
- Main apps:
  - `core`: shared models/services/admin plumbing.
  - `syncer`: GitHub ingestion, cursors/backfills, Celery sync tasks.
  - `analyzer`: derived queue/revision/dependency state and snapshots.
  - `api`: DRF views/serializers for queueboard surfaces.
  - `zulip_bot`: Zulip webhook/command integration and policies.
- Keep new modules inside the owning app (`models/`, `services/`, `tasks/`, `management/commands/`, `tests/`).
- App-specific guidance:
  - `qb_site/syncer/AGENTS.md` for ingestion, discovery/backfill, and sync admin workflows.
  - `qb_site/analyzer/AGENTS.md` for revision/queue/dependency sweeps and analytics models.
  - `qb_site/zulip_bot/AGENTS.md` for webhook/command/policy/registration behavior.

## Core Commands
```bash
uv run ruff check qb_site
uv run ruff format qb_site
uv run python qb_site/manage.py makemigrations
uv run python qb_site/manage.py migrate
uv run python qb_site/manage.py test syncer
bash scripts/repo_check_compose.sh
```

### Useful local Django commands
```bash
uv run python qb_site/manage.py runserver 0:8000
uv run python qb_site/manage.py collectstatic
uv run python qb_site/manage.py test syncer
uv run python qb_site/manage.py test analyzer
uv run python qb_site/manage.py test api
uv run python qb_site/manage.py test zulip_bot
```

## Testing and Sandbox Notes
- `bash scripts/repo_check_compose.sh` is the canonical full test/check script for this repo.
- That script starts Docker Compose services (Postgres/Redis/web) and may fail in sandboxed or restricted environments.
- If Docker/Compose is unavailable:
  - run non-DB checks (`ruff`, GraphQL validation, pure-Python tests where applicable),
  - run targeted tests that do not require the DB,
  - or ask the user to run `scripts/repo_check_compose.sh` and share results.
- When reporting verification, explicitly state what was and was not runnable.

## Migration and DB Notes
- Generate migration files on the host and commit them; do not create migrations from inside containers.
- Running `makemigrations` on host may emit a Postgres connection warning when DB is not running; file generation still works.
- PostgreSQL is the only supported DB backend for Django runtime/testing in this repo.

## Syncer/Analyzer Notes
- Repo-level sync runs through `syncer.sync_repo_since`; per-PR ingest is `syncer.sync_pr`.
- CreatedAt history backfill state is tracked by `syncer.RepoBackfillCursor`; keep that distinct from any updatedAt discovery/watermark state.
- Queue/revision sweeps in analyzer are periodic Celery tasks; keep task outputs concise and idempotent for retries.

## Operational Notes
- Compose mounts repository code read-only into containers (`.:/app:ro`) and writes runtime artifacts under `/data` volume.
- Prefer settings/env-driven behavior changes over hardcoded constants in tasks/services.
- Celery worker/beat run in Compose and rely on `PYTHONPATH=/app/qb_site:/app`.

## Agent Notes
- Do not hand-write migration files.
- If sandbox restrictions prevent Compose-based checks, run non-DB checks and ask the user to run `scripts/repo_check_compose.sh` for full validation.

## Documentation
- Keep architectural plans/decisions in `docs/design-decisions/`.
- For larger changes, use the living-plan workflow described in `docs/design-decisions/README.md`.
