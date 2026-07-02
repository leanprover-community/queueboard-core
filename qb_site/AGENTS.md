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
  - `qb_site/api/AGENTS.md` for public API endpoints, common patterns, and authentication notes.
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
- Backup policy coverage is enforced by `scripts/validate_backup_policy.py` and runs as part of `scripts/repo_check_compose.sh`.
- When adding/removing Django tables in backup scope, update `scripts/backup_policy.py` in the same change.
- That script starts Docker Compose services (Postgres/Redis/web) and may fail in sandboxed or restricted environments.
- If Docker *is* available but you want fast, focused Django tests (sandbox or
  local) without building/running the full `web` image, run the tests on the host
  against the dockerized Postgres:
  ```bash
  # 1. Ensure only the DB is up (publishes 127.0.0.1:5432; creds from docker-compose.yml).
  docker compose up -d db
  # 2. Run host tests with CI settings, pointing the DB env at the container.
  DJANGO_SETTINGS_MODULE=qb_site.settings.ci \
  DJANGO_DB_HOST=127.0.0.1 DJANGO_DB_PORT=5432 \
  DJANGO_DB_NAME=queueboard DJANGO_DB_USER=queueboard DJANGO_DB_PASSWORD=queueboard \
  uv run python qb_site/manage.py test syncer            # or a dotted path to one module
  ```
  Gotchas:
  - Use `DJANGO_DB_HOST=127.0.0.1`, not `localhost` — `localhost` resolves to `::1`
    (IPv6) first and fails with `connection refused` / `no password supplied`.
  - The compose `queueboard` role is a Postgres superuser, so Django can create the
    `test_queueboard` database — no extra grants needed.
  - A bare host invocation does NOT load `.env`, so tests that build a real
    `GitHubClient` (e.g. `test_commit_history_tasks.py`) fail with "GitHub token not
    found". Export `GH_TOKEN`/`GITHUB_TOKEN` (or run just those modules inside
    Compose, which loads `.env` via `env_file`).
  - This is for quick iteration only; `scripts/repo_check_compose.sh` stays canonical
    (it also runs migrations via the `migrate` service and backup-policy validation).
- If Docker/Compose is unavailable:
  - run non-DB checks (`ruff`, GraphQL validation, pure-Python tests where applicable),
  - run targeted tests that do not require the DB,
  - or ask the user to run `scripts/repo_check_compose.sh` and share results.
- When reporting verification, explicitly state what was and was not runnable.

## Concurrent Writers and Unique Keys
Celery workers overlap: per-PR tasks (`syncer.sync_pr`, `analyzer.process_pr`), periodic
sweeps, admin actions, and management commands can all write the same rows at the same
time. Sweeps even *prefer* recently-changed PRs, so they collide with the per-PR task for
exactly the same row. Never assume your code path is the only writer.

**Rule: for any model with a unique constraint, "check if it exists, then create" is a
bug.** Between the check and the insert, another worker can create the row, and the
resulting `IntegrityError` poisons the enclosing transaction (and, in a sweep, kills the
whole run). This applies equally to `filter(...).first()` + `create(...)` and to
snapshot-diff loops that collect `to_create` lists for `bulk_create`.
`select_for_update()` does not help here — it cannot lock a row that does not exist yet.

Use one of these instead (all are in the codebase as reference patterns):

- `get_or_create` / `update_or_create` with lookup kwargs that **exactly cover the unique
  constraint** (extra values go in `defaults`; constraint fields must not be in
  `defaults`). Django retries the get under a savepoint on conflict, so this is
  race-safe. For case-insensitive keys, put the `__iexact` lookup in kwargs and the
  concrete value in `defaults` (see `syncer/services/sub/labels_sync.py`).
- `bulk_create(..., ignore_conflicts=True)` for insert-only paths where losing the race
  needs no follow-up (see `syncer/services/sub/labels_sync.py` PRLabel attach).
- `bulk_create(..., update_conflicts=True, unique_fields=[...], update_fields=[...])`
  for upserts where both writers compute the row from the same source data and
  last-writer-wins is correct (see `analyzer/services/queue_windows.py` and
  `analyzer/services/queue_window_build_state.py`).
- Savepoint + catch `IntegrityError` + re-fetch and fall through to the update path,
  when you need the created-vs-updated distinction or custom conflict handling (see
  `syncer/services/sub/ci_sync.py:_archive_mode_upsert`,
  `syncer/services/sub/core_entities_sync.py:upsert_user_from_github`). The
  `with transaction.atomic():` around the `create()` is mandatory — without the
  savepoint, catching the error leaves the outer transaction unusable.

Additional expectations:
- Wrap multi-statement rebuilds (delete + create + update of a derived row set) in
  `transaction.atomic()` so readers and crashes never observe a half-written state.
- Sweep tasks must contain per-item failures (`except IntegrityError: log, count,
  continue`) so one conflicted row cannot abort the rest of the run — see
  `analyzer/tasks/rebuild_queue_windows_sweep.py` and the `prs_conflict_skipped`
  counter it reports.
- When adding a `UniqueConstraint` to an existing model, audit every write site for the
  patterns above in the same change.

## Migration and DB Notes
- Generate migration files on the host and commit them; do not create migrations from inside containers.
- Running `makemigrations` on host may emit a Postgres connection warning when DB is not running; file generation still works.
- PostgreSQL is the only supported DB backend for Django runtime/testing in this repo.

## Keeping Django Admin in Sync With Models
- Each app registers its models in `qb_site/<app>/admin.py` (currently `core`,
  `syncer`, `analyzer`). When you add, rename, or remove a model field, update
  the corresponding admin's `list_display`, `list_filter`, `search_fields`, and
  `readonly_fields` in the same change so operators see the new field on both
  the changelist and the detail view.
- New models should get a registration in the owning app's `admin.py`. Most of
  this codebase uses the local `ReadOnlyAdmin` base class — match that pattern
  unless there's a specific reason to allow edits.
- For new periodic-task / convergence-style metrics, add the new column to the
  relevant `*Snapshot` admin's `list_display` and `readonly_fields` so the
  metric is visible without having to write SQL.

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
