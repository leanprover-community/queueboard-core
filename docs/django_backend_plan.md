# Queueboard Django Backend Migration Plan

See also: docs/legacy_data_surface.md for an overview of the legacy pipeline’s data surface and flow used to inform the new models, and docs/syncer_ingestion_plan.md for the v1 syncer ingestion architecture and service layout.

## Status Update
- Django project, layered settings, and Compose (web/worker/beat/db/redis) are in place; Celery beat drives syncer schedules and code is mounted read-only in containers.
- Core models (`Repository`, `User`, `ReviewerPreference`) with constraints and syncer upsert helpers are implemented and exercised by the ingestion commands.
- Syncer raw schema, rate-aware tasks, discovery/backfill flows, admin tools, and metrics snapshots are live; `sync_pr_from_file` and repo/PR tasks ingest GraphQL bundles end-to-end.
- Analyzer has PR revision windows, admin actions, and commands to rebuild revisions and enqueue missing CI; CI/queue-at-time rollups remain to be built.
- API app exists but only serves a placeholder index; DRF endpoints and queueboard JSON replacements have not been built yet.
- The legacy `src/queueboard` pipeline still generates dashboards from downloaded JSON; migration plan to Django APIs is below.

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
- **Core**: define canonical objects (repository, user), milestone as needed, plus timestamp mixins and enums shared across apps. Keep curated config here (e.g., ReviewerPreferences); avoid GitHub‑owned state.
- **Syncer raw schema**: tables for pull requests, labels (definitions), PR↔label attachments, timeline events, check runs, and commit statuses; persist provider IDs where helpful for idempotency. PullRequest starts keyed by `(repository, number)` and can add GitHub IDs later if needed.
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

### Core Model: User
- Purpose: canonical person entity mapped to GitHub (and optionally Zulip) across apps.
- Fields:
  - GitHub: `github_node_id` (str, unique, nullable), `github_login` (str, case‑insensitive unique, nullable), `name` (str, nullable), `avatar_url` (URL, nullable).
  - Zulip: `zulip_user_id` (int, unique, nullable), `zulip_full_name` (str, nullable). Single realm assumed for v1.
  - Common: `timezone` (IANA tz name, str, nullable), `is_active` (bool), `created_at`, `updated_at` (timestamps).
- Constraints:
  - Unique `github_node_id` when present.
  - Case‑insensitive unique on `github_login` (functional unique constraint on `Lower(github_login)` when present).
  - Unique `zulip_user_id` when present (single Zulip realm assumption).
- Upserts:
  - Prefer matching by `github_node_id`; fallback to case‑insensitive `github_login` and backfill `github_node_id`.
  - For Zulip, once `zulip_user_id` is known, match/update by it; `zulip_full_name` is display‑only.
- Rationale: stable identity keys enable consistent joins from syncer/analyzer/API without leaking provider specifics into other tables.

### Core Model: ReviewerPreference
- Purpose: repo‑scoped reviewer preferences; used by suggestion logic and admin UIs.
- Fields:
  - `repository` (FK → Repository), `user` (FK → User); unique together.
  - `maximum_capacity` (int, default 10), `auto_assign` (bool),
  - `away_until` (timezone‑aware datetime, nullable) for temporary breaks; skip reviewer while `now_utc < away_until`.
  - `preferred_labels` (JSON list[str]) for topic/area preferences (GitHub label names).
  - `free_form` (text, nullable) for notes from reviewer‑topics.json.
- Constraints: unique `(repository, user)`.
- Import: map fields from `reviewer-topics.json` (capacity, auto_assign, temporary_break → rotation/breaks, top_level → preferred_labels, free_form → notes).
- Validation: management command to warn about unknown `preferred_labels` vs. ingested labels (see design decision 003).

Reviewer preferences import (management command)
- Command: `import_reviewer_topics`
- Args:
  - `--repo OWNER/NAME` (optional; defaults to `leanprover-community/mathlib4`)
  - `--path PATH` (optional; defaults to `reviewer-topics.json` at repo root)
  - `--dry-run` to preview changes
  - `--replace-labels` (default) vs. `--merge` to control preferred label updates
  - `--create-missing-users` (default true) to create `User` rows by GitHub login if absent
  - `--create-missing-repo-default-branch` (optional; default `master`) used only if the repo row is missing
  - `--verbose` for per-row detail
- Behavior:
  - Repo-scoped upsert of `ReviewerPreference` per mapping above; case-insensitive matching for GitHub logins and label name dedupe
  - Non-blocking label validation when syncer labels exist

### Syncer Model: PullRequest
- Purpose: raw PR entity with minimal fields required to reproduce the current queueboard.
- Fields:
  - Identity: `repository` (FK → core.Repository), `number` (int, unique with repository), `author` (FK → core.User, nullable).
  - State/timing: `state` (`open`/`closed`), `is_draft` (bool), `gh_created_at` (datetime), `gh_updated_at` (datetime), `closed_at`/`merged_at` (datetime, nullable).
  - Branches: `base_ref_name` (str), `head_ref_name` (str), `head_repo_owner_login` (str), `head_repo_name` (str).
  - Content/sizes: `title` (str), `body` (text), `additions` (int), `deletions` (int), `changed_files_count` (int).
  - Ingestion: `last_synced_at` (datetime, nullable).
- Derived/not stored initially: `from_fork` (compute from `head_repo_owner_login` vs repo owner), `html_url` (format from owner/name/number), `mergeable_state` (defer; labels drive merge-conflict today).
- Constraints/indexes:
  - Unique `(repository, number)`.
  - Index `(repository, state)` for “open PRs” filters.
  - Index `(repository, gh_updated_at)` to support recency sorts within a repo.
- Notes:
  - CI, labels, assignees, reviews, comments, and timeline events live in separate syncer tables joined during queue/dashboard computation.

### Syncer Model: LabelDef
- Purpose: per-repository label catalog used for PR label attachments and filtering.
- Fields:
  - `repository` (FK → core.Repository)
  - `name` (str) — display casing from GitHub, stored as-is
  - `color` (str, 6 hex digits, no leading `#`)
- Constraints/indexes:
  - Case-insensitive unique on `(repository, lower(name))` to reflect GitHub’s case-insensitive labels while preserving display casing.
- Notes:
  - No description field; URL derivable if needed. Validation is deferred; we accept API-provided values as-is.

### Syncer Model: PRLabel
- Purpose: current label attachments for PRs (no history — timeline events will capture add/remove for analytics).
- Fields:
  - `pull_request` (FK → syncer.PullRequest)
  - `label_def` (FK → syncer.LabelDef)
- Constraints/indexes:
  - Unique `(pull_request, label_def)` to prevent duplicates.
  - Index on `pull_request` to speed EXISTS/NOT EXISTS probes from PR when filtering by allowed/blocked labels.
  - Index on `label_def` to support “all PRs with label X” lookups.
- Notes:
  - Case-insensitive label identity is enforced at LabelDef via `(repository, lower(name))`; PRLabel only references those canonical rows.

### Syncer Model: PRTimelineEvent
- Purpose: store only the timeline events needed to replay PR status evolution.
- Event types (enum): `LABELED`, `UNLABELED`, `READY_FOR_REVIEW`, `CONVERT_TO_DRAFT`, `REOPENED`, `CLOSED`.
- Fields:
  - `pull_request` (FK → syncer.PullRequest)
  - `github_node_id` (str, nullable) — GraphQL timeline item id, used for idempotent upserts
  - `type` (enum)
  - `occurred_at` (datetime, tz-aware)
  - `label_name` (str, nullable; only for LABELED/UNLABELED; stored as-is for historical fidelity)
- Constraints/indexes:
  - Conditional unique on `github_node_id` when present.
  - Index on `(pull_request, occurred_at)` to support chronological replay by PR.
- Notes:
  - We intentionally avoid an FK from `label_name` to `LabelDef` to keep ingestion fast, tolerate gaps, and preserve historical names independent of label catalog renames; classification will canonicalize names as needed.

### Syncer Model: CheckRun
- Purpose: snapshot of the latest check runs per commit context (via GraphQL statusCheckRollup) used to classify current CI; run-level history (multiple attempts) can be added later.
- Fields:
  - `pull_request` (FK → syncer.PullRequest)
  - `github_node_id` (str, unique) — GraphQL node id
  - `head_sha` (str), `name` (str)
  - `status` (QUEUED/IN_PROGRESS/COMPLETED), `conclusion` (SUCCESS/FAILURE/CANCELLED/NEUTRAL/SKIPPED/TIMED_OUT/ACTION_REQUIRED, nullable)
  - `details_url` (url, nullable), `external_id` (str, nullable)
  - Timestamps: `gh_started_at` (nullable), `gh_completed_at` (nullable)
    - Note: GitHub GraphQL CheckRun does not expose `updatedAt`; we omit `gh_updated_at` and rely on `gh_completed_at` for ordering.
  - Ingestion: `last_synced_at` (nullable, updated on every CI ingest/refresh so we know when we last heard about this run from GitHub)
- Indexes:
  - `(pull_request, gh_completed_at)` for chronological scans

### Syncer Model: StatusContext
- Purpose: per-commit context statuses. In v1 we ingest latest snapshots for each context from GraphQL rollup.
- Fields:
  - `pull_request` (FK → syncer.PullRequest)
  - `github_node_id` (str, unique, nullable) — GraphQL snapshot id
  - `head_sha` (str), `name` (context name), `state` (SUCCESS/FAILURE/ERROR/PENDING)
  - `target_url` (url, nullable), `description` (text, nullable)
  - Timestamp: `gh_created_at` (datetime)
  - Ingestion: `last_synced_at` (nullable, updated on every CI ingest/refresh so we know when we last heard about this context from GitHub)
- Indexes:
  - `(pull_request, gh_created_at)` for chronological scans

Analyzer ownership: coarse CI transitions
- Analyzer will materialize `PRCIStatusEvent (pull_request, occurred_at, ci_status)` from CheckRun + StatusContext and the repo-configurable “inessential jobs” list. This keeps Syncer focused on raw facts and allows us to evolve classification rules without ingestion changes.


## Service Architecture
- Port existing scraping logic into `syncer.services` with interfaces like `PullRequestSyncService`; wrap GitHub API access behind clients that manage rate limits, retries, and ETag caching.
- Introduce background execution (Celery, RQ, or Django-Q) to schedule sync cycles and analytics recomputation, with periodic tasks for incremental and full refresh runs.
- In `analyzer.services`, implement pipelines that transform raw tables into analytics tables, with checkpoints to avoid duplicate work.
- Capture domain events (e.g., sync completed) to trigger downstream analytics tasks, and keep orchestration idempotent.
- Provide management commands for manual runs (`sync_github`, `build_analytics`, `refresh_dashboards`).

Developer utilities (current)
- A file‑based ingestion command is available to ingest a single PR bundle JSON (from GraphQL) for development and fixtures:
  - `qb_site/manage.py sync_pr_from_file --repo OWNER/NAME --file PATH [--dry-run]`
  - Uses the sub‑sync services to upsert PR core, labels, timeline events, and CI snapshots. `--dry-run` rolls back writes.
- Sub‑sync services live under `qb_site/syncer/services/sub/` and are covered by unit tests. See `docs/syncer_ingestion_plan.md` for the query and layout.
 - Core entity sub‑services:
   - `upsert_repo_metadata` persists `core.Repository.github_node_id` and `default_branch` from the bundle, with optional rename support (disabled by default).
   - `upsert_user_from_github` creates/updates `core.User` for PR authors by GitHub node id or login, updating `name` and `avatar_url` when provided.

## API Layer
- Status: the API app currently exposes only the placeholder index view; DRF wiring and endpoints remain to be built.
- Adopt Django REST Framework for serialization, viewsets, filtering, pagination, throttling.
- Namespace routes under `/api/` with versioning (`/api/v1/`); expose raw entities as needed and focused analytics endpoints consumed by the frontend.
- Implement composite responses for dashboard widgets (queue snapshot, reviewer load, trend summaries).
- Enable caching headers and optional Redis cache for high-traffic endpoints.
- Provide schema documentation via `drf-spectacular` or `drf-yasg`, published alongside existing docs.

## Queueboard API Migration Plan
- Goal: replace the filesystem JSON “backend” in `src/queueboard` with Django/DRF endpoints while keeping current consumers stable during the cutover.
- Phase 1: document the contract and version it — freeze the shapes used in `api/*.json` (`AggregatePRInfo`, `BasicPRInformation`, `PRStatus`, `CIStatus`, reviewer suggestions, dependency graph) and publish them under `/api/v1/queueboard/...`.
- Phase 2: close data gaps in Postgres — confirm Syncer tables contain every field the legacy pipeline uses (direct dependencies parsed from descriptions, reviewer-topic fields, CI rollups, label kinds) and backfill from existing bundle fixtures where needed.
- Phase 3: port classification/suggestion logic — move `classify_pr_state`, `ci_status`, state-evolution timing, and reviewer suggestion logic into Django services (Analyzer) with unit tests mirroring `src/queueboard/test_state_evolution.py` and fixture-based parity checks.
- Phase 4: build a snapshot endpoint — DRF view that emits the same payloads as today’s `api/*.json` (`aggregate_info`, `draft_PRs`, `nondraft_PRs`, `CI_status`, `base_branch`, `all_pr_status`, `prs_to_list`, `automatic_assignments`, `area_stats`, `dependency_graph`), with ETags/Last-Modified and optional Redis caching.
- Phase 5: precompute and cache — periodic Celery task to materialize a `QueueSnapshot` (or reuse Analyzer snapshot tables) so the API mostly serves prebuilt JSON blobs; add admin/management commands for forced refresh during debugging.
- Phase 6: bridge the client — add an optional `--source api` path to `src/queueboard/dashboard_data.py` (or a thin adapter) that fetches the snapshot endpoint and writes the same `api/*.json` artifacts; run dual pipelines in CI and diff outputs to prove parity.
- Phase 7: flip and deprecate — switch the default generator to the API source once parity is proven, keep filesystem download behind a flag for one release, then retire the legacy download/processing scripts.

## Testing and CI
- Standardize on pytest + pytest-django; configure coverage and type-checking (mypy or pyright).
- Add factory fixtures (factory-boy) for models; seed baseline data for integration tests covering sync + analytics flows.
- Create smoke tests for API endpoints and regression tests for analytics calculations.
- Update GitHub Actions workflow to run linting (ruff, mypy), tests, migrations, and build Docker images if applicable.
- Collect sample fixtures from existing scraped data to validate migration parity.
- Compose checks: use `scripts/repo_check_compose.sh` in CI to run various system checks inside Docker Compose against a real Postgres.
 - Current test entrypoints (Django test runner): `docker compose exec -T web python qb_site/manage.py test syncer`.

## Data Migration and Operations
- Write import scripts to load historical JSON/CSV dumps into the new raw tables (bulk create, upsert by GitHub ID).
- Validate analytics regeneration against legacy outputs before switching production consumers.
- Plan rollback: keep legacy scraping workflow runnable until Django pipelines reach parity; introduce feature flags for API clients.
- Add operational dashboards/alerts for sync failures, latency, and data freshness.
- Document deployment steps, secrets management, infrastructure requirements (DB, cache, worker, static hosting), and provide a runbook for on-call triage.
- Docker Compose: use a dedicated `migrate` service that runs `python qb_site/manage.py migrate --noinput` and mark `web`/`worker`/`beat` as depending on it with `condition: service_completed_successfully`. Avoid running migrations from multiple long‑lived services concurrently.
- Developer note: running `makemigrations` on the host without Postgres may emit a RuntimeWarning
  about a refused DB connection or missing password. This is harmless and migrations are still created;
  use Docker Compose (or set local DB env vars) if you prefer a warning‑free run.

## Remaining Work (near‑term)
1. Queueboard API migration
    - Finalize the versioned contract, build the DRF snapshot endpoint + Celery precompute, add the `--source api` adapter for the legacy generator, run dual pipelines to diff outputs, then flip the default and retire filesystem downloads.
2. CI backfill across force‑pushes (Analyzer‑driven)
    - Build `PRRevision` windows from force‑push events and head state.
    - Identify historical SHAs missing CI and enqueue Syncer CI fetches (rate‑aware scheduling remains in Syncer).
    - Add query helpers for “CI at time T” and “who was on the queue at T”.
3. Metrics and observability
    - Keep token usage from `rate_events`; consider adding rollups for commit/timeline backfill pages if needed.
    - Small admin summary for per‑repo task volumes and token cost over selectable windows.
4. Admin ergonomics
    - Optional per‑run overrides for backfill budgets on PR enqueue.
    - Quick links from PR admin to filtered Task Results (already present; iterate as needed).
5. Tests
    - Extend backfill tests (commit + timeline) and rate‑guard paths.
    - Add Analyzer unit tests for revision windows and CI reconstruction.

## Syncer Scheduling (Current Functionality)
- Dispatcher + per-repo tasks:
  - Celery beat schedules `syncer.sync_active_repos` every `SYNCER_ACTIVE_REPOS_PERIOD_SECONDS` (default 300s).
  - The dispatcher enqueues `syncer.sync_repo_since(repo_id)` for each active repository.
  - The repo task discovers changed PRs since a sliding lookback (`SYNCER_DISCOVERY_LOOKBACK_MINUTES`) and enqueues `syncer.sync_pr` for each number. Discovery states and limits are configurable.
  - Celery beat also schedules:
    - `syncer.backfill_repo_history_active` (default hourly) which enqueues createdAt-based history backfill for all active repositories.
    - `syncer.backfill_repo_incomplete_prs_active` (default hourly, small per-repo limit) which enqueues incomplete-PR backfill for all active repositories.
    - `syncer.refresh_pending_ci_for_active_repos` (default hourly, small per-repo limit) which enqueues pending-CI refresh for all active repositories.
- Concurrency controls:
  - Per-repo Postgres advisory lock ensures no overlapping runs for the same repo.
  - Rate-aware continuation implemented; when budget is low we stop early and schedule continuation at `resetAt` (debounced via Redis). A global single‑token lock is not used in the current design.
- State and watermarks:
  - V1 uses a sliding discovery window; per PR, `PullRequest.last_synced_at` remains the single watermark.
  - No persisted discovery cursors in V1; idempotent upserts and repeated windows are acceptable.
- Interfaces:
  - Admin: Repository → Tools → “Enqueue repo-level sync task” form.
  - CLI: `manage.py enqueue_repo_sync --repo owner/name [--since ... --limit ... --states ...]`.
  - Periodic: driven by beat as configured in settings.

## Syncer: Current Functionality
- Rate-aware continuation and guards
  - `syncer.sync_repo_since` stops early when `remaining <= SYNCER_RATE_REMAINING_MIN` and schedules a continuation at `resetAt + jitter` (debounced).
  - `syncer.sync_pr` guards optional pagination/backfill when budget is low; on mid-sync low budget it defers to `resetAt` instead of failing.
- Backfill improvements
  - Timeline backfill on up-to-date runs with `SYNCER_TIMELINE_BACKFILL_PAGES`; persists `timeline_backfill_cursor/done/earliest_synced_at`.
  - Commit backfill on both up-to-date and synced runs with `SYNCER_COMMITS_BACKFILL_PAGES`; persists `commits_backfill_cursor/done/earliest_synced_at`.
  - Repository history backfill via `syncer.backfill_repo_history`:
    - Uses `GitHubClient.get_prs_created_page` ordered by `CREATED_AT ASC` and a per-repo `RepoBackfillCursor` to ensure every PR in a repository is eventually synced at least once.
    - Continues to run even after initially reaching the end of history so that newly created PRs (e.g., during downtime) are picked up once createdAt-based backfill resumes.
- Admin polish
  - PR page shows backfill fields and inlines for associated events/checks/statuses; object tools to enqueue sync (respects backfill defaults).
  - Task Results list shortens IDs, hides unused group results, and removes Add.
  - Repository admin:
    - “Sync tools” page adds a “History backfill” button to enqueue a `backfill_repo_history_task` for a single repository.
    - Changelist includes an action “Enqueue history backfill for selected repositories” which enqueues `backfill_repo_history_task` per selected repo.
- Metrics
  - `SyncerMetricsSnapshot` (15-minute) captures task counts and token usage; an admin button triggers ad-hoc collection.

## Environment
- `SYNCER_RATE_REMAINING_MIN`, `SYNCER_TIMELINE_K_DEFAULT`, `SYNCER_COMMITS_M_DEFAULT`
- `SYNCER_TIMELINE_BACKFILL_PAGES`, `SYNCER_COMMITS_BACKFILL_PAGES`
- History backfill:
  - `SYNCER_HISTORY_BACKFILL_PAGE_SIZE`, `SYNCER_HISTORY_BACKFILL_MAX_PAGES`
  - `SYNCER_HISTORY_BACKFILL_STATES_DEFAULT`, `SYNCER_HISTORY_BACKFILL_PERIOD_SECONDS`
- Incomplete-PR backfill:
  - `SYNCER_INCOMPLETE_BACKFILL_PERIOD_SECONDS`, `SYNCER_INCOMPLETE_BACKFILL_LIMIT`
- Discovery:
  - `SYNCER_DISCOVERY_LOOKBACK_MINUTES`, `SYNCER_DISCOVERY_LIMIT`, `SYNCER_DISCOVERY_STATES_DEFAULT`
 - Pending-CI refresh:
   - `SYNCER_PENDING_CI_MAX_AGE_HOURS`
   - `SYNCER_PENDING_CI_REFRESH_PERIOD_SECONDS`
   - `SYNCER_PENDING_CI_REFRESH_MAX_PRS`
   - `SYNCER_PENDING_CI_REFRESH_MAX_SHAS_PER_PR`
- `SYNCER_REPO_ENQUEUE_BATCH_MAX`, `SYNCER_EST_COST_PER_PR`

## Planned Additions (Analyzer)
- CI-at-time helpers and queue computations (`ci_state_at_time`, `queue_state_at_time`).
- Daily results with rules versioning (`QueueDailySnapshot`, `PRQueueDailySpan`) and a `QueueRuleSet` model.
- Optional periodic coordinator for steady CI backfill using revision windows and Syncer rate-aware tasks.

## Analyzer Progress
- Models/services in place:
  - `PRRevision` model with indexes and ordering.
  - `rebuild_pr_revisions(pr)` builds windows from timeline events (seeding from CI when needed).
  - `PRDependency` + `PRDependencyState` parse body checkboxes into dependencies; `analyzer.process_pr` rebuilds per-PR, and periodic `analyzer.rebuild_dependencies_sweep` (builder-versioned, fan-out capable) backfills/refreshes incrementally with state tracking.
- `next_revision_backfill_shas(pr, limit)` identifies head SHAs with missing or only pending/queued CI.
- Syncer counterpart ready to consume requests:
  - `sync_ci_for_shas(repo_id, number, shas=[...], max_pages_per_sha=?, require_pr_association=?)` with rate guard and continuation.
 - Admin & commands:
   - Read‑only PRRevision admin view and inline on PR pages.
   - PR object tools: Analyzer “Rebuild revisions”, “Enqueue missing CI”, and “Rebuild dependencies” (links/actions).
   - Commands: `rebuild_revisions` and `plan_ci_backfill` (dry‑run and enqueue modes).
