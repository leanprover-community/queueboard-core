# Site Analytics Ingestion (`site_analytics`)

## Context
- Lightweight, privacy-preserving pageview analytics for funder-facing growth reporting on static community sites.
- No new infrastructure: implemented as a Django app inside the existing `qb_site` stack (Postgres, Celery, Redis).
- Privacy constraint: no raw IP retention; visitor identity approximated by a monthly-rotating hash.
- Operational constraint: ingestion endpoint must be fast, resilient, and callable from third-party static sites (CORS required).

## Decision
- New Django app `site_analytics` with three models, a REST ingestion endpoint, and three Celery periodic tasks.
- Site allowlist (`SITE_ANALYTICS_ALLOWED_SITES`) gates ingestion; no per-site auth tokens in v1.
- Reporting reads from aggregate tables only; raw rows are bounded-retention scratch space.

## Architecture

### Models
- `AnalyticsPageView` — raw event rows; immutable after insert; pruned after `SITE_ANALYTICS_RETENTION_DAYS` (default 540).
  - Fields: `site`, `path`, `referrer`, `user_agent`, `occurred_at`, `visitor_month_hash`.
  - Indexes: `(site, occurred_at)`, `(occurred_at)`.
- `AnalyticsDailyMetric` — daily aggregate per site; unique on `(site, date)`.
  - Fields: `site`, `date` (UTC), `pageviews`, `unique_visitors`.
- `AnalyticsMonthlyMetric` — monthly aggregate per site; unique on `(site, month)`.
  - Fields: `site`, `month` (UTC first-of-month `DateField`, e.g. `2026-03-01`), `pageviews`, `unique_visitors`.

### Ingestion endpoint
- `POST /api/v1/analytics/collect` — view in `api/views/analytics_collect.py`.
- Required fields: `site` (must be in `SITE_ANALYTICS_ALLOWED_SITES`), `path`.
- Optional field: `referrer`.
- `User-Agent` read from HTTP header (not payload).
- Returns `204` on success, bot drop, and empty-UA drop; `400` on validation failure.
- CORS headers (`Access-Control-Allow-Origin: *`) on all responses; `OPTIONS` preflight handled.
- No CSRF enforcement: DRF `authentication_classes = []` / `permission_classes = []`.

### Privacy
- Raw IP not stored. `visitor_month_hash = sha256(ip | normalized_ua | YYYY-MM | salt)`.
- Fields joined with `|` to prevent cross-field hash collisions.
- UA is lowercased before hashing so casing variation in the same browser does not inflate unique-visitor counts.
- IP extracted from `X-Forwarded-For` (leftmost address; set by Heroku routing and most reverse proxies), falling back to `REMOTE_ADDR` for direct connections.
- `month_key` is UTC `YYYY-MM`; cross-month correlation is impossible by construction.
- Salt from `SITE_ANALYTICS_HASH_SALT` env var. **Changing the salt or separator invalidates all historical hashes.**

### Bot filtering
- Substring denylist in `site_analytics/services/bot_filter.py` matched against lowercased UA.
- Bots return `204` (not a distinct error code) to avoid leaking detection heuristics.
- Optional empty-UA rejection via `SITE_ANALYTICS_REJECT_EMPTY_UA=1` (default off); also returns `204`.

### Aggregation
- `site_analytics.aggregate_daily_metrics` (default: hourly) — upserts `AnalyticsDailyMetric` for a rolling `days_back=2` window so events near UTC midnight are never missed. Fully idempotent.
- `site_analytics.aggregate_monthly_metrics` (default: daily) — upserts `AnalyticsMonthlyMetric` for a rolling `months_back=2` window.
- `site_analytics.prune_old_pageviews` (default: daily) — deletes `AnalyticsPageView` rows older than the retention cutoff. Aggregate tables are never pruned.
- All three tasks preserve existing aggregate rows when the corresponding raw rows have been pruned, to avoid silently zeroing out reporting data after the retention window.

### Backup policy
- `site_analytics_analyticspageview` → `TRUNCATE_TABLES` (raw rows contain visitor hashes).
- `site_analytics_analyticsdailymetric`, `site_analytics_analyticsmonthlymetric` → `RETAIN_TABLES` (aggregate-only, safe to share publicly).

## Invariants
- Reporting reads must come from aggregate tables, not raw pageview scans.
- Hashing semantics (field separator `|`, UA normalization, month key format, salt) are part of the privacy contract; any change requires an explicit migration note and bumps all historical hashes.
- Aggregation tasks must remain idempotent and safe under retries and overlapping runs.
- All date/time boundaries use UTC. `occurred_at__date=d` with `USE_TZ=True` and `TIME_ZONE=UTC` evaluates at UTC midnight in PostgreSQL.
- `site` slugs must remain stable; renames require an explicit backfill of both raw rows and aggregate tables.

## Operational Notes

### Key settings (all env-overridable)
| Setting | Default | Notes |
|---|---|---|
| `SITE_ANALYTICS_HASH_SALT` | `""` | Required in production; empty disables hash safety in dev |
| `SITE_ANALYTICS_ALLOWED_SITES` | `""` | Comma-separated slugs; empty list rejects all traffic |
| `SITE_ANALYTICS_RETENTION_DAYS` | `540` | ~18 months of raw row retention |
| `SITE_ANALYTICS_DAILY_AGGREGATE_PERIOD_SECONDS` | `3600` | Set to `0` to disable |
| `SITE_ANALYTICS_MONTHLY_AGGREGATE_PERIOD_SECONDS` | `86400` | Set to `0` to disable |
| `SITE_ANALYTICS_PRUNE_PERIOD_SECONDS` | `86400` | Set to `0` to disable |
| `SITE_ANALYTICS_REJECT_EMPTY_UA` | `0` | Set to `1` for stricter bot hardening |

### Onboarding a new site
1. Add the site slug to `SITE_ANALYTICS_ALLOWED_SITES` (comma-separated, no spaces).
2. Deploy/restart the web dyno so the new slug is live.
3. Add the tracking snippet to the site (see below).
4. Add a visible privacy notice to the site informing visitors that anonymous visit counts are collected (no cookies, no IP addresses stored). See the snippet notes below for the recommended wording.
5. Verify events appear in the Django admin under `AnalyticsPageView`.
6. After one aggregation cycle, check `AnalyticsDailyMetric` for counts.

### Static-site tracking snippet

Place this snippet at the bottom of each page (or in a shared layout template).
Replace `YOUR_QUEUEBOARD_HOST` and `YOUR_SITE_SLUG` before deploying.

```html
<script>
(function () {
  var endpoint = 'https://YOUR_QUEUEBOARD_HOST/api/v1/analytics/collect';
  var payload = JSON.stringify({
    site: 'YOUR_SITE_SLUG',
    path: window.location.pathname,
    referrer: document.referrer || ''
  });
  // sendBeacon fires even during page unload; fetch is the fallback.
  if (navigator.sendBeacon) {
    navigator.sendBeacon(endpoint, new Blob([payload], { type: 'application/json' }));
  } else {
    fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: payload,
      keepalive: true
    }).catch(function () {});
  }
})();
</script>
```

**Notes:**
- The snippet is fire-and-forget; errors are silently swallowed so a tracking failure never affects page load.
- `sendBeacon` is preferred: it survives page unload and does not block navigation.
- No cookies, no persistent identifiers, no third-party scripts.
- The endpoint returns `204` for all non-error outcomes (success, bot drop, unknown UA) so the response body is never read.
- **Disclosure requirement:** Sites using this snippet must inform visitors that anonymous visit counts are collected. The recommended notice text is: *"This page collects anonymous visit counts for usage reporting (no cookies, no IP addresses stored)."* The queueboard dashboard injects this notice automatically alongside the snippet; other sites should add equivalent wording to their footer or privacy statement.

### Migrations
- `0001_initial` — `AnalyticsPageView`
- `0002_analyticsdailymetric` — `AnalyticsDailyMetric`
- `0003_analyticsmonthlymetric` — `AnalyticsMonthlyMetric`

## Consequences
- Adds ~48 DB tables to backup scope (three new, classified in policy).
- Ingestion is synchronous (one DB write per request); at high volume a write buffer or async insert could be considered.
- Unique-visitor counts are approximate: same visitor across different browsers or after a UA update will be counted separately; this is acceptable for coarse funder reporting.
- Monthly hash rotation means a visitor who spans a month boundary is counted as two unique visitors. This is by design to prevent cross-month correlation.

## Deferred (v1.1)
- Per-site auth tokens.
- Per-path monthly aggregates (`top_paths_json` field on `AnalyticsMonthlyMetric`).
- Top-referrer aggregates (`top_referrers_json`).
- DRF throttle policy on the collection endpoint.
- Partitioning `AnalyticsPageView` by month at higher volumes.

## References
- `qb_site/site_analytics/` — app source
- `qb_site/api/views/analytics_collect.py` — ingestion view
- `qb_site/qb_site/settings/base.py` — beat schedule and settings
- `scripts/backup_policy.py` — table classification
- `docs/design-decisions/016-sanitized-backups.md`
