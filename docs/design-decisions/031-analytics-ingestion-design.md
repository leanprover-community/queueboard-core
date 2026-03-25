# Analytics Ingestion for `qb_site` (Living Plan)

## Context
- We want lightweight website analytics for funder-facing growth reporting.
- Current `qb_site` structure already has clear boundaries:
  - `api/` for HTTP surface area.
  - `syncer/` for raw GitHub ingestion.
  - `analyzer/` for derived analytics built from stored facts.
- Existing Celery beat and snapshot patterns in `qb_site/qb_site/settings/base.py` and `analyzer/tasks/*` are good templates.
- The previous version of this doc was generic and not mapped to repo-specific modules, rollout, or tests.

## Problem Statement
- Add privacy-preserving pageview ingestion for static properties without introducing new infrastructure.
- Keep implementation operationally simple and consistent with the current Django/Celery architecture.
- Make implementation trackable as a chunked, testable plan that can be updated during delivery.

## Goals / Non-Goals
- Goals:
  - Collect coarse pageview/referrer signals for multiple sites.
  - Produce daily and monthly aggregates suitable for funder reporting.
  - Enforce privacy constraints (no raw IP retention, no persistent cross-month identifiers).
  - Keep ingestion endpoint lightweight and resilient.
- Non-goals (v1):
  - Sessionization, funnels, attribution modeling.
  - Cookie-based or long-lived user identity.
  - Real-time dashboarding.
  - New data infrastructure (Kafka/ClickHouse/etc.).

## Decision (Current Plan)
- Implement a new Django app: `site_analytics`.
- Expose ingestion under existing API namespace via `qb_site/api/urls.py`.
- Store raw event rows in `site_analytics` (bounded retention), and store reporting reads in aggregated tables.
- Run periodic aggregation with Celery beat, following existing analyzer/syncer task style.
- Keep auth simple in v1: optional per-site shared token + bot/user-agent filtering.

## Proposed Design

### App and module placement
- New app: `qb_site/site_analytics/`
  - `models/`: raw + aggregate tables.
  - `services/`: hashing, bot filtering, aggregation logic.
  - `tasks/`: periodic aggregation + retention.
  - `tests/`: model/service/task coverage.
- API entrypoint:
  - Add endpoint in `qb_site/api/urls.py`.
  - View implementation in `qb_site/api/views/analytics_collect.py`.
  - This keeps external endpoints discoverable in one API module.

### HTTP ingestion contract
- Endpoint: `POST /api/v1/analytics/collect`
- Request payload (v1):
  - `site` (slug; required; must be in `SITE_ANALYTICS_ALLOWED_SITES`)
  - `path` (required)
  - `referrer` (optional)
- Behavior:
  - Uses DRF `APIView` with `authentication_classes = []` and `permission_classes = []`; this implicitly bypasses CSRF enforcement without needing `@csrf_exempt`.
  - Minimal synchronous work: validate, compute hash fields, write one row, return `204`.
  - Reject unknown `site` or malformed payloads with `400`.

### Data model (planned)
- `AnalyticsPageView` (raw)
  - `site`, `path`, `referrer`, `user_agent`
  - `occurred_at` (event time, default `timezone.now`)
  - `visitor_month_hash` (privacy-preserving monthly hash)
  - indexes on `(site, occurred_at)`, `(occurred_at)`, and optionally `(site, path, occurred_at)`
- `AnalyticsDailyMetric` (aggregate)
  - `site`, `date`
  - `pageviews`, `unique_visitors`
  - unique constraint on `(site, date)`
- `AnalyticsMonthlyMetric` (aggregate/reporting convenience)
  - `site`, `month`
  - `pageviews`, `unique_visitors`, `top_referrers_json`, `top_paths_json` (optional in v1)
  - unique constraint on `(site, month)`

### Privacy and identity strategy
- Do not persist raw IP addresses.
- Compute `visitor_month_hash = sha256(ip + normalized_user_agent + month_key + secret_salt)`.
- `month_key` is UTC `YYYY-MM`; this intentionally prevents cross-month correlation.
- `secret_salt` comes from env/config (e.g., `SITE_ANALYTICS_HASH_SALT`).
- IP extraction: read `X-Forwarded-For` first (set by Heroku's routing layer and most reverse proxies); fall back to `REMOTE_ADDR`. Take the first (leftmost) address from `X-Forwarded-For` to get the client IP.
- Retain raw rows only for bounded backfill/debug windows (target: 12-18 months).

### Aggregation strategy
- Add periodic tasks:
  - `site_analytics.aggregate_daily_metrics` (hourly or nightly; idempotent upsert).
  - `site_analytics.aggregate_monthly_metrics` (daily; recompute current + previous month).
  - `site_analytics.prune_old_pageviews` (daily retention cleanup).
- Wire schedules in `qb_site/qb_site/settings/base.py` with env-overridable intervals and retention days.

### Security and abuse controls
- Site allowlist via `SITE_ANALYTICS_ALLOWED_SITES` env var (comma-separated slugs); unknown slugs rejected with `400`.
  - Per-site tokens deferred to v1.1.
- Basic bot filtering:
  - denylist common bot user-agent substrings.
  - optional reject when user-agent is empty.
- Optional DRF/Django rate limiting for collection endpoint (can begin with app-level simple limits).

### Public sanitized backup compatibility
- This data will flow through the public backup pipeline in `.github/workflows/upload_backup.yaml`.
- Keep analytics tables and fields compatible with sanitization/export scripts (`scripts/sanitize_backup.py`, `scripts/export_for_analysis.py`).
- Backup policy for analytics tables:
  - `site_analytics_analyticspageview` → `TRUNCATE_TABLES` (raw rows contain visitor hashes; exclude from public backup).
  - `site_analytics_analyticsdailymetric`, `site_analytics_analyticsmonthlymetric` → `RETAIN_TABLES` (aggregate-only, safe to share).
- `scripts/backup_policy.py` must be updated in A1 alongside the migration; `repo_check_compose.sh` enforces coverage.
- Treat this as a release gate for analytics schema changes, consistent with `docs/design-decisions/016-sanitized-backups.md`.

## Invariants / Subtleties
- Reporting reads must come from aggregate tables, not raw pageview scans.
- Hashing semantics are part of the privacy contract; any change requires explicit migration/versioning note.
- Aggregation tasks must be idempotent and safe under retries/overlap.
- Time boundary semantics must use UTC consistently for day/month buckets.
- `site` taxonomy must remain stable; renames need explicit backfill/mapping handling.

## Implementation Plan (Chunks)
1. `A1` App scaffold + settings wiring.
   - Create `site_analytics` app and add to `INSTALLED_APPS`.
   - Add `AnalyticsPageView` raw model and initial migration.
   - Add env settings: `SITE_ANALYTICS_HASH_SALT`, `SITE_ANALYTICS_ALLOWED_SITES` (comma-separated slugs), `SITE_ANALYTICS_RETENTION_DAYS`, task period vars.
   - Update `scripts/backup_policy.py` with the three new tables.
   - Create `qb_site/site_analytics/AGENTS.md` (and `CLAUDE.md`).
2. `A2` Raw ingestion endpoint + validation.
   - Add `POST /api/v1/analytics/collect` route and view.
   - Implement payload validation, site allowlist check, IP extraction (X-Forwarded-For → REMOTE_ADDR fallback), hashing service, and raw insert.
   - Per-site tokens deferred to v1.1.
3. `A3` Daily aggregate model + service + Celery task.
   - Add `AnalyticsDailyMetric` model, upsert service, and `site_analytics.aggregate_daily_metrics` Celery task.
   - Wire beat schedule entry in `base.py`.
4. `A4` Monthly aggregate model + service + Celery task + prune task.
   - Add `AnalyticsMonthlyMetric` model, upsert service, and `site_analytics.aggregate_monthly_metrics` Celery task.
   - Add `site_analytics.prune_old_pageviews` task.
   - Wire beat schedule entries.
5. `A5` Admin + operational visibility.
   - Register admin for raw/aggregate models.
   - Add concise task result payloads/counters.
6. `A6` Retention and hardening.
   - Tune bot filtering and optional throttle policy.

## Validation Plan
- Unit tests:
  - hashing/privacy rules, month rotation behavior.
  - bot-filter decisions and payload validation.
  - aggregation correctness (`COUNT(*)`, distinct hash counts).
- API tests (`qb_site/api/tests/`):
  - `204` success path.
  - `400` invalid payload.
  - `403` invalid token (when enabled).
- Task tests:
  - idempotent reruns.
  - retry-safe partial failure behavior.
  - retention pruning boundaries.
- Integration checks:
  - `uv run ruff check qb_site`
  - `uv run ruff format qb_site`
  - Compose-backed tests via `bash scripts/repo_check_compose.sh` when available.

## Rollout Plan
- Phase 0: dark launch ingestion in one low-risk site.
- Phase 1: enable daily/monthly aggregation and validate numbers manually for 1-2 weeks.
- Phase 2: onboard remaining static sites and publish recurring reporting output.
- Phase 3: tighten retention and evaluate need for partitioning at higher volumes.

## Progress Notes
- 2026-02-26:
  - Converted this file from generic guidance to a repo-specific living plan.
  - Anchored implementation to existing `qb_site` boundaries and task patterns.
  - No code implementation started yet; all chunks currently pending.
- 2026-03-25:
  - Pre-implementation design review; resolved open questions and refined plan:
    - CSRF: implicit via DRF `authentication_classes = []`, no decorator needed.
    - Site config: `SITE_ANALYTICS_ALLOWED_SITES` comma-separated env var; per-site tokens deferred to v1.1.
    - IP extraction: `X-Forwarded-For` (Heroku/proxy) → `REMOTE_ADDR` fallback in hashing service.
    - Backup policy: raw pageviews truncated, daily/monthly aggregates retained; `backup_policy.py` update required in A1.
    - `tasks/__init__.py` re-export pattern (consistent with syncer/analyzer) added to A1 scaffold.
    - `AGENTS.md` creation required in A1.

## Open Questions
- ~~Should `site` configuration live in DB (admin-editable) or settings/env (static)?~~ Resolved: settings/env (`SITE_ANALYTICS_ALLOWED_SITES`) for v1.
- Do we need per-path monthly aggregates in v1, or can we defer to v1.1?
- What default retention window is acceptable for privacy/compliance expectations?
- ~~Should endpoint auth be required for all sites from day one, or optional during bootstrap?~~ Resolved: no per-site tokens in v1; `SITE_ANALYTICS_ALLOWED_SITES` allowlist is the only gate.

## References
- `docs/design-decisions/README.md`
- `.github/workflows/upload_backup.yaml`
- `docs/design-decisions/016-sanitized-backups.md`
- `docs/design-decisions/030-sync-task-dedupe-strategy.md`
- `docs/design-decisions/029-updatedat-discovery-watermark-and-catchup.md`
- `qb_site/qb_site/urls.py`
- `qb_site/api/urls.py`
- `qb_site/qb_site/settings/base.py`
- `qb_site/analyzer/tasks/collect_convergence.py`
