# Site Analytics Guidelines

## Scope
- `qb_site/site_analytics/` implements privacy-preserving pageview ingestion and aggregation for static/funder-facing sites.
- Raw events in `AnalyticsPageView`; aggregate reporting in `AnalyticsDailyMetric` and `AnalyticsMonthlyMetric` (added in A3/A4).
- Design record: `docs/design-decisions/031-analytics-ingestion-design.md`.

## Module Layout
- `models/pageview.py` — `AnalyticsPageView` raw event rows (immutable after insert).
- `models/daily_metric.py` — `AnalyticsDailyMetric` (added in A3).
- `models/monthly_metric.py` — `AnalyticsMonthlyMetric` (added in A4).
- `models/salt.py` — `SiteAnalyticsSalt` single-row table holding the current month's hash salt.
- `services/` — hashing, bot filtering, aggregation logic.
- `checks.py` — Django system checks (registered in `apps.py:ready()`).
- `tasks/` — periodic Celery tasks for aggregation, pruning, and salt rotation.
- `tests/` — unit and integration tests.
- API ingestion view: `qb_site/api/views/analytics_collect.py` (added in A2).

## Key Settings (all env-overridable)
- `SITE_ANALYTICS_HASH_SALT` — fallback salt used until the first `rotate_salt` task runs and writes a DB salt. Required on first deploy; thereafter the `SiteAnalyticsSalt` DB row takes precedence.
- `SITE_ANALYTICS_ALLOWED_SITES` — comma-separated site slugs; unknown slugs rejected with `400`.
- `SITE_ANALYTICS_RETENTION_DAYS` — raw pageview retention window (default 540 days / ~18 months).
- `SITE_ANALYTICS_TRUSTED_PROXY_COUNT` — reverse-proxy hops in front of the app (default 1, matching Heroku's router). Controls how many `X-Forwarded-For` entries from the right are trusted; 0 ignores the header entirely.
- `SITE_ANALYTICS_DAILY_AGGREGATE_PERIOD_SECONDS` — beat period for daily aggregation task (default 3600).
- `SITE_ANALYTICS_MONTHLY_AGGREGATE_PERIOD_SECONDS` — beat period for monthly aggregation task (default 86400).
- `SITE_ANALYTICS_PRUNE_PERIOD_SECONDS` — beat period for retention pruning task (default 86400).

## Task Surface
Celery task names (as registered via `@shared_task(name=…)`):

- `site_analytics.aggregate_daily_metrics` — idempotent upsert of daily pageview/unique-visitor counts (added in A3).
- `site_analytics.aggregate_monthly_metrics` — idempotent upsert of monthly metrics; recomputes current + previous month (added in A4).
- `site_analytics.prune_old_pageviews` — deletes raw rows older than `SITE_ANALYTICS_RETENTION_DAYS` (added in A4).
- `site_analytics.rotate_salt` — generates a new random visitor-hash salt and discards the previous one; runs at midnight UTC on the 1st of each month.

## Privacy Invariants
- Raw IP addresses are never stored.
- **Ingestion fails closed without a salt.** `compute_visitor_hash` raises `SaltUnavailable` when neither a `SiteAnalyticsSalt` row nor `SITE_ANALYTICS_HASH_SALT` is set, and the collect view drops the event (204 + error log) rather than persist an unsalted hash — `sha256(ip | ua)` with no secret is brute-forceable over the IPv4 space, so it would be a recoverable identifier, not a pseudonymous one. Collecting nothing is the correct failure mode.
- A deploy-time system check (`site_analytics.E001`, in `checks.py`) fails `manage.py check`/`migrate` when `SITE_ANALYTICS_ALLOWED_SITES` is non-empty but no salt is set. It is gated on allowed-sites because analytics is opt-in, and reads settings only — never the DB, since `migrate` runs checks before `SiteAnalyticsSalt` exists. Gunicorn does not run system checks on boot, so the runtime guarantee is `SaltUnavailable`, not this check.
- `visitor_month_hash = sha256(ip | normalized_user_agent | salt)` where `salt` is the current month's randomly generated value from `SiteAnalyticsSalt`.
- The salt is replaced at month start and the old value deleted, so hashes from different months are unlinkable even with knowledge of the current salt (forward secrecy).
- IP is extracted from `X-Forwarded-For` taking `SITE_ANALYTICS_TRUSTED_PROXY_COUNT` entries from the **right** (proxies append; the leftmost entries are client-supplied and spoofable), falling back to `REMOTE_ADDR`. Set the count to 0 when the app is exposed directly.
- Changing hashing semantics requires an explicit migration/versioning note in the design doc.

## Backup Policy
- `site_analytics_analyticspageview` → TRUNCATE (raw rows contain visitor hashes; excluded from public backup).
- `site_analytics_analyticsdailymetric`, `site_analytics_analyticsmonthlymetric` → RETAIN (aggregate-only, safe to share).
- Update `scripts/backup_policy.py` whenever adding or removing tables.

## Testing
```bash
uv run python qb_site/manage.py test site_analytics
bash scripts/repo_check_compose.sh
```
