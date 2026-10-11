# API App Guidelines

## Scope
- `qb_site/api/` serves read-only JSON endpoints consumed by queueboard frontends and external integrations.
- All endpoints are unauthenticated and publicly accessible.
- Views live in `views/`; no serializers are currently used (views return raw JSON via DRF `Response` or `HttpResponse`).

## Endpoints

Endpoints are read-only (`GET`, and `HEAD`), except `/api/v1/analytics/collect`, which takes `POST` only. Most
require a `?repo=owner/name` query parameter.

| URL | View | Description |
|-----|------|-------------|
| `/api/` | `index` | Plain-text index response. No `repo` param needed. Touches nothing, so it cannot tell a broken database from a working one. |
| `/api/health` | `health` | Health check of the database and the Celery broker: 200 or 503, per-check status. No `repo` param needed. See below. |
| `/api/v1/queueboard/snapshot` | `QueueboardSnapshotView` | Cached queue snapshot for a repo. Supports `cache_key`, `rule_set_id`, `refresh`. |
| `/api/v1/queueboard/dependency-graph` | `QueueboardDependencyGraphView` | PR dependency graph derived from a cached snapshot. |
| `/api/v1/queueboard/automatic-assignments` | `ReviewerAssignmentsView` | Automatic reviewer assignments derived from a cached snapshot. Supports `cache_key`, `rule_set_id`, `refresh`. |
| `/api/v1/queueboard/area-stats` | `AreaStatsView` | Area stats derived from a cached snapshot. Supports `cache_key`, `rule_set_id`, `refresh`. |
| `/api/v1/reviewer-interests` | `ReviewerInterestsView` | Public reviewer interests (GitHub login, preferred labels, free-form text) for a repo. Intended for external consumers such as the community website. |

## Common Patterns
- `repo` query parameter is required (format: `owner/name`); returns 400 if missing or malformed, 404 if the repo is not in the DB.
- Snapshot-backed endpoints (`snapshot`, `automatic-assignments`, `area-stats`, `dependency-graph`) return 202 if no snapshot exists yet and enqueue a background build task.
- Snapshot-backed endpoints support HTTP conditional requests (`ETag`/`If-None-Match`, `Last-Modified`/`If-Modified-Since`) and return 304 when content is unchanged.
- `X-Queueboard-Stale: 1` is set when a snapshot is stale but still returned; a background refresh is enqueued.
- `X-Queueboard-Refresh-Task` carries the Celery task ID when a refresh is enqueued.

## Health check
`/api/health` is for container health checks and external uptime monitors.

- Runs `SELECT 1` on the default database connection and a Redis `PING` on `CELERY_BROKER_URL`
  (through `syncer.services.rate_budget._get_redis_client`).
- 200 `{"status": "ok", "checks": {"database": "ok", "broker": "ok"}}` when both pass; otherwise 503
  with `"status": "error"` and each failed check as `"error"`. `Cache-Control: no-store`.
- Each check gives up after about 2 s (`CHECK_TIMEOUT_SECONDS`): a statement timeout on the query,
  and connect/socket timeouts on the Redis client. Opening the database connection is not covered
  by that: Django opens one per request, bounded by `DJANGO_DB_CONNECT_TIMEOUT` (libpq's
  `connect_timeout`, default 10 s, per address the host resolves to). Without it psycopg would wait
  130 s for a server that accepts the connection but never answers. A refused connection fails at once.
- The response never carries exception text, hostnames or connection details: each failed check is
  logged at WARNING by `api.views.health`, as one line with the exception's type and message (no
  traceback, since a monitor probes an outage repeatedly).
- A plain Django view, not DRF: no content negotiation (the browsable API page would set a CSRF
  cookie), and it never reads the session, so a probe, which sends no cookies, gets no session or
  CSRF cookie back. (A browser that sends a session cookie has its session saved, as on every path,
  because of `SESSION_SAVE_EVERY_REQUEST`.)
- `GET` and `HEAD` only; anything else gets 405. The view is CSRF-exempt, since it takes no unsafe
  method; otherwise the CSRF middleware would answer a `POST` with 403 first.
- It is still subject to `ALLOWED_HOSTS`, so a probe from inside a container needs `localhost` in
  `DJANGO_ALLOWED_HOSTS`.
- Site analytics only counts what the static sites POST to `/api/v1/analytics/collect`, so probes
  never show up there.

## Authentication
- `authentication_classes = []`, `permission_classes = []` on all DRF views — no auth required.

## Privacy
- Everything served here is public. Never expose private reviewer preferences —
  `ReviewerPreference.conflict_of_interest` and `excluded_labels` (design doc 057) — or anything
  derived per reviewer from them (e.g. assignment-engine traces, which name the reviewers each rule
  filtered). `test_reviewer_interests.py` pins the interests endpoint's field set.

## Testing
```bash
uv run python qb_site/manage.py test api
```
