# Site Analytics Ingestion (`site_analytics`)

## Context
- Lightweight, privacy-preserving pageview analytics for funder-facing growth reporting on static community sites.
- No new infrastructure: implemented as a Django app inside the existing `qb_site` stack (Postgres, Celery, Redis).
- Privacy constraint: no raw IP retention; visitor identity approximated by a monthly-rotating hash.
- Operational constraint: ingestion endpoint must be fast, resilient, and callable from third-party static sites (CORS required).

## Decision
- New Django app `site_analytics` with four models, a REST ingestion endpoint, and four Celery periodic tasks.
- Site allowlist (`SITE_ANALYTICS_ALLOWED_SITES`) gates ingestion; no per-site auth tokens in v1.
- Reporting reads from aggregate tables only; raw rows are bounded-retention scratch space.
- No rate limiting in v1 — first rollout is to internally-used sites. See [Deferred: rate limiting](#deferred-rate-limiting-on-the-collection-endpoint) for the rationale, the trigger to revisit, and the intended shape.
- Ingestion fails closed rather than degrading: with no salt available, events are dropped, not stored under a weak hash.

## Architecture

### Models
- `AnalyticsPageView` — raw event rows; immutable after insert; pruned after `SITE_ANALYTICS_RETENTION_DAYS` (default 540).
  - Fields: `site`, `path`, `referrer`, `user_agent`, `occurred_at`, `visitor_month_hash`.
  - Indexes: `(site, occurred_at)`, `(occurred_at)`.
- `AnalyticsDailyMetric` — daily aggregate per site; unique on `(site, date)`.
  - Fields: `site`, `date` (UTC), `pageviews`, `unique_visitors`.
- `AnalyticsMonthlyMetric` — monthly aggregate per site; unique on `(site, month)`.
  - Fields: `site`, `month` (UTC first-of-month `DateField`, e.g. `2026-03-01`), `pageviews`, `unique_visitors`.
- `SiteAnalyticsSalt` — single live row holding the current month's visitor-hash salt; previous row deleted on rotation.

### Ingestion endpoint
- `POST /api/v1/analytics/collect` — view in `api/views/analytics_collect.py`.
- Required fields: `site` (must be in `SITE_ANALYTICS_ALLOWED_SITES`), `path`.
- Optional field: `referrer`.
- `User-Agent` read from HTTP header (not payload).
- Returns `204` on success, bot drop, and empty-UA drop; `400` on validation failure.
- CORS headers (`Access-Control-Allow-Origin: *`) on all responses; `OPTIONS` preflight handled.
- No CSRF enforcement: DRF `authentication_classes = []` / `permission_classes = []`.

### Privacy
- Raw IP not stored. `visitor_month_hash = sha256(ip | normalized_ua | salt)`.
- Fields joined with `|` to prevent cross-field hash collisions.
- UA is lowercased before hashing so casing variation in the same browser does not inflate unique-visitor counts.
- IP extracted from `X-Forwarded-For` taking `SITE_ANALYTICS_TRUSTED_PROXY_COUNT` entries from the **right** (default 1), falling back to `REMOTE_ADDR` for direct connections. Proxies *append* the address they received the connection from, so with one hop (Heroku's router) the rightmost entry is the only trustworthy one; everything to its left is a client-supplied claim. Reading the leftmost entry — as v1 originally did — let any visitor send an arbitrary `X-Forwarded-For` and mint a fresh `visitor_month_hash` per request, inflating unique-visitor counts at zero cost. Set the count to `0` when the app is exposed directly, which ignores the header entirely.
- Cross-month correlation is prevented by monthly salt rotation: the salt is replaced at the start of each month and the old value discarded, so hashes from different months are unlinkable even with knowledge of the current salt (forward secrecy against salt leakage).
- Salt is stored in `SiteAnalyticsSalt` (DB, one live row). `SITE_ANALYTICS_HASH_SALT` env var is the fallback until the first rotation task runs. **Changing the separator invalidates all historical hashes.**
- **Ingestion fails closed with no salt.** `compute_visitor_hash` raises `SaltUnavailable` when neither source yields a salt, and the view drops the event (`204` + error log) rather than persist the hash. An unsalted `sha256(ip | ua)` is brute-forceable across the IPv4 space, so it is a recoverable identifier, not a pseudonymous one — collecting nothing is the correct failure mode for this pipeline. This is the runtime guarantee, and it also covers the salt row disappearing after boot.
- A deploy-time system check (`site_analytics.E001`, `site_analytics/checks.py`) fails `manage.py check` and `migrate` when `SITE_ANALYTICS_ALLOWED_SITES` is non-empty but no salt is configured, so a misconfigured deploy stops in the release phase. It is gated on allowed-sites because analytics is opt-in (CI and dev are unaffected), and reads settings only — never the DB, since `migrate` runs checks before `SiteAnalyticsSalt` exists. Gunicorn does not run system checks when loading the WSGI app, which is why the check alone is not sufficient.

### Bot filtering
- Substring denylist in `site_analytics/services/bot_filter.py` matched against lowercased UA.
- Bots return `204` (not a distinct error code) to avoid leaking detection heuristics.
- Optional empty-UA rejection via `SITE_ANALYTICS_REJECT_EMPTY_UA=1` (default off); also returns `204`.

### Aggregation
- `site_analytics.aggregate_daily_metrics` (default: hourly) — upserts `AnalyticsDailyMetric` for a rolling `days_back=2` window so events near UTC midnight are never missed. Fully idempotent.
- `site_analytics.aggregate_monthly_metrics` (default: daily) — upserts `AnalyticsMonthlyMetric` for a rolling `months_back=2` window.
- `site_analytics.prune_old_pageviews` (default: daily) — deletes `AnalyticsPageView` rows older than the retention cutoff. Aggregate tables are never pruned.
- `site_analytics.rotate_salt` (monthly, midnight UTC on the 1st) — generates a new random salt, saves it to `SiteAnalyticsSalt`, and deletes the previous row atomically.
- All three tasks preserve existing aggregate rows when the corresponding raw rows have been pruned, to avoid silently zeroing out reporting data after the retention window.

### Backup policy
- `site_analytics_analyticspageview` → `TRUNCATE_TABLES` (raw rows contain visitor hashes).
- `site_analytics_analyticsdailymetric`, `site_analytics_analyticsmonthlymetric` → `RETAIN_TABLES` (aggregate-only, safe to share publicly).
- `site_analytics_siteanalyticssalt` → `TRUNCATE_TABLES` (contains the live secret; must never appear in sanitized backups).

## Invariants
- Reporting reads must come from aggregate tables, not raw pageview scans.
- Hashing semantics (field separator `|`, UA normalization, salt source) are part of the privacy contract; any change requires an explicit migration note and bumps all historical hashes.
- A visitor hash is never persisted without a salt. Any future caller of `compute_visitor_hash` must let `SaltUnavailable` propagate or drop the event — never fall back to an unsalted digest.
- Client-supplied headers are never trusted for visitor identity. `X-Forwarded-For` is attacker-controlled to the left of the hops we actually run behind; anything deriving identity (hashing today, rate-limit keys later) must go through `get_client_ip` rather than reading the header directly.
- Aggregation tasks must remain idempotent and safe under retries and overlapping runs.
- All date/time boundaries use UTC. `occurred_at__date=d` with `USE_TZ=True` and `TIME_ZONE=UTC` evaluates at UTC midnight in PostgreSQL.
- `site` slugs must remain stable; renames require an explicit backfill of both raw rows and aggregate tables.

## Operational Notes

### Key settings (all env-overridable)
| Setting | Default | Notes |
|---|---|---|
| `SITE_ANALYTICS_HASH_SALT` | `""` | Bootstrap salt until `rotate_salt` first runs. Empty **drops all events** (fail closed), and fails `check`/`migrate` when allowed-sites is set |
| `SITE_ANALYTICS_ALLOWED_SITES` | `""` | Comma-separated slugs; empty list rejects all traffic (and disables the salt check) |
| `SITE_ANALYTICS_RETENTION_DAYS` | `540` | ~18 months of raw row retention |
| `SITE_ANALYTICS_TRUSTED_PROXY_COUNT` | `1` | Proxy hops in front of the app; trusts that many `X-Forwarded-For` entries from the right. `0` ignores the header |
| `SITE_ANALYTICS_DAILY_AGGREGATE_PERIOD_SECONDS` | `3600` | Set to `0` to disable |
| `SITE_ANALYTICS_MONTHLY_AGGREGATE_PERIOD_SECONDS` | `86400` | Set to `0` to disable |
| `SITE_ANALYTICS_PRUNE_PERIOD_SECONDS` | `86400` | Set to `0` to disable |
| `SITE_ANALYTICS_REJECT_EMPTY_UA` | `0` | Set to `1` for stricter bot hardening |

### Onboarding a new site
1. Add the site slug to `SITE_ANALYTICS_ALLOWED_SITES` (comma-separated, no spaces).
2. Deploy/restart the web dyno so the new slug is live.
3. Add the tracking snippet to the site (see below).
4. Add a visible privacy notice to the site informing visitors that anonymous visit counts are collected (no cookies, no IP addresses stored), linking the operating entity's privacy policy. See disclosure notes below.
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

### Disclosure and privacy regulations

This system is designed to minimise regulatory obligations, but the picture is nuanced enough to warrant documentation. *This section is informational, not legal advice.*

**ePrivacy Directive (EU, [2002/58/EC](https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:02002L0058-20091219) Art. 5(3)) and UK PECR (SI 2003/2426 Reg. 6)** — these rules require consent for *storing information in, or gaining access to information already stored in, the user's terminal equipment*. This system performs server-side hash computation on data transmitted in the HTTP request (IP address from the network layer, `User-Agent` header) and writes nothing to the user's device (no cookies, no localStorage, no fingerprinting scripts). The [EDPB Guidelines 2/2023 on the Technical Scope of Art. 5(3)](https://www.edpb.europa.eu/our-work-tools/our-documents/guidelines/guidelines-22023-technical-scope-art-53-eprivacy-directive_en) (adopted October 2024) take a broad view: they state that gaining access to IP addresses triggers Art. 5(3) "in cases where this information originates from the terminal equipment of a subscriber or user." Whether passively transmitted HTTP request metadata (as opposed to data actively read from device storage) falls under this scope is not conclusively settled. The [ICO's guidance on storage and access technologies](https://ico.org.uk/for-organisations/direct-marketing-and-privacy-and-electronic-communications/guidance-on-the-use-of-storage-and-access-technologies/) defines PECR Regulation 6 as covering technologies that "store information on a user's device or gain access to information on a user's device." On balance, a system that neither stores nor reads from the device is likely outside the scope of Art. 5(3) / Reg. 6, but this is an area of evolving regulatory interpretation.

**GDPR / UK GDPR ([Regulation 2016/679](https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32016R0679))** — whether GDPR applies turns on whether the monthly rotating hash constitutes "personal data" under Art. 4(1). Recital 26 excludes "anonymous information" from GDPR's scope, and provides a "means reasonably likely" test: whether identification is feasible given "all objective factors, such as the costs of and the amount of time required for identification, taking into consideration the available technology." The CJEU's ruling in [*Breyer v. Bundesrepublik Deutschland* (C-582/14, 2016)](https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:62014CJ0582) established that dynamic IP addresses can constitute personal data, but only where the controller has "the legal means which enable it to identify the data subject with additional data which the internet service provider has about that person." In this system the raw IP is never stored and the salt is secret and rotated monthly, which is a strong argument for anonymity under Recital 26. The cautious position treats the hash as pseudonymous personal data; in that case, Art. 6(1)(f) legitimate interests is the appropriate lawful basis for coarse usage analytics — **no consent is required** — but Art. 13 transparency obligations apply, meaning visitors must be able to find information about the processing (e.g., via a linked privacy statement or footer notice).

**[CNIL guidance](https://www.cnil.fr/fr/cookies-solutions-pour-les-outils-de-mesure-daudience) (France)** — the CNIL's framework for consent-exempt analytics covers tools that use short-lived identifiers with immediate IP anonymisation, provided the data is used solely for audience measurement, does not enable cross-site tracking, and is retained for no more than 25 months. This system satisfies those conditions. The CNIL still expects users to be informed of the tracking (e.g., via the site's privacy policy), even for consent-exempt tools.

**Practical position:** No consent banner is required. As a matter of good practice — and to satisfy GDPR Art. 13 under the cautious reading that the hash is personal data — sites using this snippet should make a brief privacy notice accessible to visitors. The recommended notice text is: *"This page collects anonymous visit counts for usage reporting (no cookies, no IP addresses stored)."* The queueboard dashboard injects this notice automatically alongside the snippet, followed by a link to the data controller's full privacy policy (`PRIVACY_POLICY_URL` in `src/queueboard/dashboard.py`, currently <https://mathlib-initiative.org/privacy/>); other sites should add equivalent wording to their footer or privacy statement, linking their own controller's policy.

### Migrations
- `0001_initial` — `AnalyticsPageView`
- `0002_analyticsdailymetric` — `AnalyticsDailyMetric`
- `0003_analyticsmonthlymetric` — `AnalyticsMonthlyMetric`
- `0004_siteanalyticssalt` — `SiteAnalyticsSalt`

## Consequences
- Adds ~48 DB tables to backup scope (three new, classified in policy).
- Ingestion is synchronous (one DB write per request); at high volume a write buffer or async insert could be considered.
- Unique-visitor counts are approximate: same visitor across different browsers or after a UA update will be counted separately; this is acceptable for coarse funder reporting.
- Monthly hash rotation means a visitor who spans a month boundary is counted as two unique visitors. This is by design to prevent cross-month correlation.

## Deferred (v1.1)
- Per-site auth tokens.
- Per-path monthly aggregates (`top_paths_json` field on `AnalyticsMonthlyMetric`).
- Top-referrer aggregates (`top_referrers_json`).
- Partitioning `AnalyticsPageView` by month at higher volumes.

### Deferred: rate limiting on the collection endpoint

**Decision.** Ship v1 without a throttle. First rollout targets sites used mainly
internally, where the incentive to inflate counts and the cost exposure from an
anonymous write endpoint are both close to nil.

**Trigger to revisit — going public / funder-facing, not a date.** That is when both
risks actually appear. Two properties make the deferral safe until then:

- *It is additive.* Throttling is entirely server-side: same endpoint, same payload,
  same tracking snippet. Since the snippet is baked into generated Pages HTML by a
  workflow in the `queueboard` repo, anything requiring a client change (tokens,
  batching, backoff) would be the expensive kind of retrofit. This is not that.
- *Adding a cache later has no blast radius.* Nothing in the project currently uses
  `django.core.cache` (the in-process dicts in `core`/`zulip_bot` are plain
  dictionaries), and sessions are DB-backed with no `SESSION_ENGINE` override, so
  introducing a `default` cache will not silently change existing behavior.

**Shape when we do it.** DRF `SimpleRateThrottle` subclass scoped to this view, plus a
Redis-backed `CACHES` entry on a *different* DB index from the Celery broker. Two
things must not be missed:

- Throttling is backed entirely by `django.core.cache`. On the current implicit
  per-process `LocMemCache` the effective limit becomes N × rate across N gunicorn
  workers and resets on restart — a throttle without a shared cache is decorative.
- Override `get_cache_key` to use `get_client_ip` rather than DRF's default
  `get_ident`, which (with `NUM_PROXIES` unset) keys on the whole `X-Forwarded-For`
  chain. That is client-controlled, so a caller could mint a fresh throttle bucket per
  request — the same defect this doc records for visitor hashing above. Setting
  `NUM_PROXIES = 1` also works, but reusing `get_client_ip` keeps one definition of
  caller identity.

**Scope limits, so this is not oversold.** A per-IP throttle caps a single-source
flood. It does not stop distributed inflation, nor a patient drip staying under the
limit; the endpoint is unauthenticated and CORS-open by design. Set the limit
generously — shared NAT (campus, corporate egress, mobile CGNAT) presents as one IP.

**What does not rewind.** Raw pageviews prune at `SITE_ANALYTICS_RETENTION_DAYS`, but
daily/monthly aggregate rows persist indefinitely. Repairing polluted counts means
recomputing aggregates from raw rows, which only works while those rows are still
inside the retention window. This is the concrete reason to have a throttle in place
*before* the numbers are reported to anyone, rather than after.

## References
- `qb_site/site_analytics/` — app source
- `qb_site/api/views/analytics_collect.py` — ingestion view
- `qb_site/qb_site/settings/base.py` — beat schedule and settings
- `scripts/backup_policy.py` — table classification
- `docs/design-decisions/016-sanitized-backups.md`
