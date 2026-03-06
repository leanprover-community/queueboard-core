# GitHub Webhook Ingestion for Syncer

## Context
- Sync freshness previously relied on poll/backfill loops (`sync_repo_since`, pending-CI refresh, history/incomplete backfills).
- CI reruns on the same SHA could update queue-critical CI state without timely PR-level discovery updates.
- Syncer now stores CI in commit-scoped tables and already has CI-by-SHA ingestion (`sync_ci_for_shas`), which is the right primitive for webhook-driven CI updates.
- We needed:
  - low-latency webhook ingestion for PR/check events,
  - strong idempotency and safe rollback controls,
  - preserved poller/backfill safety nets.

## Decision
- Add a GitHub webhook endpoint at `POST /webhooks/github/` and treat webhook payloads as routing/trigger signals (not a second canonical ingestion path).
- Require HMAC verification (`X-Hub-Signature-256`) using `GITHUB_WEBHOOK_SECRET`.
- Gate processing with:
  - `SYNCER_GITHUB_WEBHOOK_ENABLED` (global on/off),
  - `SYNCER_GITHUB_WEBHOOK_DRY_RUN` (route-only mode; no enqueue).
- Persist webhook delivery ledger rows in `syncer.GitHubWebhookDelivery`:
  - unique `delivery_id` for idempotency,
  - structured route summary in `summary_json`,
  - duplicate replay tracking (`duplicate_count`, `last_duplicate_at`).
- Route/fanout behavior:
  - `pull_request` events:
    - action allowlist,
    - enqueue `syncer.sync_pr` for active repos only.
  - `check_run` / `check_suite` events:
    - action allowlist,
    - resolve PR targets from local `(repo, head_sha)` open PRs plus payload PR references,
    - enqueue `syncer.sync_ci_for_shas` per resolved PR with `shas=[head_sha]`.
- For webhook-triggered CI fanout, set `trigger_analyzer_after_sync=True` so `sync_ci_for_shas` enqueues `analyzer.process_pr` after successful CI sync.
- Keep scheduled poll/backfill loops enabled as recovery/fallback mechanisms.
- Add webhook observability in `SyncerMetricsSnapshot` and admin:
  - delivery/route/reason counters,
  - duplicate replay activity (`webhook_duplicates_touched`),
  - read-only `GitHubWebhookDelivery` admin with route/reason/duplicate fields.

## Consequences
- Benefits:
  - materially faster CI freshness for active PRs, including same-SHA reruns,
  - explicit idempotency at delivery level,
  - controlled rollout (enabled flag + dry-run mode),
  - improved operational visibility in admin/metrics.
- Trade-offs:
  - webhook volume can be high for check events; action filtering and future task dedupe remain important for queue stability.
  - duplicate replay metric is a “rows touched in window” signal, not exact replay-event cardinality.
  - payload-driven direct DB writes remain intentionally limited to avoid a parallel canonical ingestion path.

## Operational Notes
- GitHub App webhook setup:
  - URL: `https://<host>/webhooks/github/`
  - enable events:
    - `Pull request`
    - `Check run`
    - `Check suite`
  - `ping` deliveries are expected.
- Recommended GitHub App repository permissions:
  - `Metadata: Read-only`
  - `Pull requests: Read-only`
  - `Checks: Read-only`
  - `Commit statuses: Read-only` (recommended compatibility)
- Runtime toggles:
  - normal mode:
    - `SYNCER_GITHUB_WEBHOOK_ENABLED=1`
    - `SYNCER_GITHUB_WEBHOOK_DRY_RUN=0`
  - rollback/debug:
    - disable processing (`...ENABLED=0`) or enable route-only mode (`...DRY_RUN=1`).
- Poller tuning after webhook adoption should be staged (do not disable fallback loops immediately).

## Alternatives
- Continue poll/backfill-only ingestion:
  - rejected due to higher latency for CI reruns and higher sustained polling load.
- Write full Syncer facts directly from webhook payloads:
  - rejected as primary path; payload completeness/consistency is not sufficient for canonical ingestion parity.
- Keep webhook endpoint without delivery ledger:
  - rejected due to weak replay/idempotency guarantees and poor operational auditability.

## References
- `qb_site/syncer/views.py`
- `qb_site/syncer/services/github_webhook_router.py`
- `qb_site/syncer/models/github_webhook_delivery.py`
- `qb_site/syncer/tasks/sync_tasks.py`
- `qb_site/syncer/models/metrics.py`
- `qb_site/syncer/tasks/metrics_tasks.py`
- `qb_site/syncer/admin.py`
- `docs/design-decisions/030-sync-task-dedupe-strategy.md`
