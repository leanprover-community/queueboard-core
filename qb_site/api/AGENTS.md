# API App Guidelines

## Scope
- `qb_site/api/` serves read-only JSON endpoints consumed by queueboard frontends and external integrations.
- All endpoints are unauthenticated and publicly accessible.
- Views live in `views/`; no serializers are currently used (views return raw JSON via DRF `Response` or `HttpResponse`).

## Endpoints

All endpoints accept `GET` only. Most require a `?repo=owner/name` query parameter.

| URL | View | Description |
|-----|------|-------------|
| `/api/` | `index` | Plain-text health/index response. No `repo` param needed. |
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

## Authentication
- `authentication_classes = []`, `permission_classes = []` on all views — no auth required.

## Testing
```bash
uv run python qb_site/manage.py test api
```
