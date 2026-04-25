# Repository Guidelines

## Project Structure & Module Organization
- `src/queueboard/` contains the legacy Python data pipeline: GraphQL queries under `queries/`, HTML assets in `static/`, and scripts like `dashboard.py`, `process.py`, and `suggest_reviewer.py`.
- `qb_site/` hosts the Django codebase; apps live in `qb_site/{core,syncer,analyzer,api,zulip_bot}/` and share settings from `qb_site/qb_site/settings/`.
- `scripts/` provides operational helpers; `test/` stores fixture JSON for dashboard regression checks; `docs/` captures architecture plans/decisions.

## Build, Test, and Development Commands
```bash
uv sync                                        # install the Python 3.12 environment pinned by uv.lock
uv run ruff check .                            # lint against ruff.toml rules (130-char lines)
uv run ruff format .                           # apply canonical formatting
uv run python -m queueboard.dashboard test/all-open-PRs-1.json test/all-open-PRs-2.json  # regenerate HTML from fixtures
uv run python src/queueboard/test_state_evolution.py                                    # run targeted state evolution tests
docker compose up --build                      # launch web + Postgres + Redis + Celery worker/beat
bash scripts/repo_check_compose.sh             # canonical full-repo checks (Compose + Postgres + Django tests)
```

Notes
- Running `makemigrations` on the host without Postgres running may emit a RuntimeWarning about
  a refused DB connection or missing password. This is harmless and migrations are still created.
- Compose runs migrations via a dedicated `migrate` service; `web`/`worker`/`beat` depend on it to avoid
  concurrent migration races.
- `scripts/repo_check_compose.sh` requires Docker/Compose and starts services; it may not be runnable in sandboxed environments.
- `scripts/repo_check_compose.sh` also validates the sanitized backup policy coverage, so schema/table removals
  must be reflected in `scripts/backup_policy.py`.
- In restricted/sandboxed environments, prefer non-DB checks (lint/format, GraphQL validation, pure-Python tests) or ask the user to run compose checks and share output.

## Coding Style & Naming Conventions
- Use four-space indentation, explicit imports, and `snake_case` for modules/functions, `PascalCase` for classes, `SCREAMING_SNAKE_CASE` for settings.
- Treat `ruff` as the source of truth; prefer `ruff --fix` or `uv run ruff format .` for autofixes before committing.
- Always format files before sending for review using: `uv run ruff format .`.
- Add type hints on new public functions, especially in Django services; reuse helpers from `queueboard.util` instead of duplicating logic.

## Testing Guidelines
- Use `bash scripts/repo_check_compose.sh` as the primary end-to-end test/check entrypoint.
- Exercise dashboard generation with the provided fixtures via `uv run python -m queueboard.dashboard ...`; capture `before/` and `after/` HTML snapshots when comparing layout changes.
- Keep unit tests colocated (`test_*.py`); expand `src/queueboard/test_state_evolution.py` or add pytest modules under the relevant Django app (`qb_site/<app>/tests/`).
- If Compose/Postgres is unavailable, run focused non-DB tests where possible and clearly call out coverage gaps.
- Document any manual data validation or backfill steps in your PR description so reviewers can reproduce the checks.

## Commit & Pull Request Guidelines
- Follow conventional commits (`feat:`, `fix:`, `chore:`, `doc:`) with subjects under 72 characters; place context and breaking notes in the body.
- Every PR should summarize motivation, list user-visible effects (screenshots, schema diffs, migrations), and describe how you tested.
- Link related issues or Zulip threads, flag follow-up TODOs, and note rollout considerations (feature flags, data backfills) when applicable.

## Configuration & Environment
- Copy `.env.example` to `.env` for local Django work; supply database credentials, GitHub tokens, and task runner settings as described in `docs/django_backend_plan.md`.
- Run the stack through `docker compose` against PostgreSQL; we no longer support SQLite fallbacks for quick tests.
- Keep secrets out of version control—store them in the `.env` file or your chosen secret manager.
- **When adding a new Django setting backed by an env var**: always add it in both `qb_site/qb_site/settings/base.py` (as `FOO = os.getenv("FOO", ...)`) *and* `.env.example` (with a comment). Omitting either means the setting silently has no effect in production or is undiscoverable for new deployments.

## Containers & Volumes
- Code is bind-mounted read-only into containers (`.:/app:ro`) to avoid writes into the repo from inside Docker.
- Runtime artifacts (Django `STATIC_ROOT`, `MEDIA_ROOT`, Celery beat schedule) write under `/data` backed by the `appdata` named volume.
- Generate migrations on the host (e.g., `uv run python qb_site/manage.py makemigrations`) and commit them, rather than creating files from inside containers.

## Design Decisions
- Architectural and operational choices are recorded under `docs/design-decisions/`.
- Start with `docs/design-decisions/README.md` for format and naming conventions.

## Keeping AGENTS.md Files Updated
- Every directory with significant logic has its own `AGENTS.md` (mirrored as `CLAUDE.md`).
  Current locations: root, `qb_site/`, `qb_site/syncer/`, `qb_site/analyzer/`,
  `qb_site/zulip_bot/`, `src/queueboard/`.
- When you add, rename, or remove management commands, Celery tasks, key services, or
  directory structure, update the relevant `AGENTS.md` in the same commit/PR.
- When you add a new app or significant sub-directory, create a matching `AGENTS.md`
  (and a `CLAUDE.md` that just contains `@AGENTS.md`) in that directory.
- Keep task names in AGENTS.md in sync with the `name=` string in `@shared_task(name=…)`
  decorators—these are the identifiers used in admin, logs, and beat schedules.
