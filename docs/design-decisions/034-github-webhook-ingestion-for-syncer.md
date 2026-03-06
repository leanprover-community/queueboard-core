# GitHub Webhook Ingestion for Syncer (Living Plan)

## Context
- Current sync freshness is primarily driven by:
  - updatedAt discovery (`syncer.sync_repo_since`),
  - periodic pending-CI refresh (`syncer.refresh_pending_ci_for_active_repos`),
  - targeted CI-by-SHA backfill (`syncer.sync_ci_for_shas`, Analyzer planners).
- We already store CI as commit-scoped facts (`CommitCheckRun`, `CommitStatusContext`) and can fetch by SHA (`syncer.services.ci_by_sha_service.sync_ci_for_sha`).
- There is no GitHub webhook endpoint in the Django URL map today (`qb_site/qb_site/urls.py` only includes admin/api/zulip routes).
- Practical gap: CI reruns on the same SHA can move fail -> success without a PR-level state change that discovery reliably catches in time.
- Operational goal: use webhooks for low-latency change signals, keep polling/backfills as correctness safety net.

## Goals / Non-Goals
- Goals:
  - Detect CI completions and reruns quickly, including same-SHA transitions.
  - Reduce dependence on polling latency for active PRs.
  - Keep existing discovery/backfill systems as fallback after downtime or missed deliveries.
  - Preserve idempotency and rate-awareness under duplicate/out-of-order events.
- Non-goals:
  - Replacing `sync_repo_since` or backfill tasks.
  - Recomputing Analyzer state synchronously in the HTTP request path.
  - Introducing exactly-once processing semantics across the whole pipeline.

## Problem Framing
- Why fail -> success reruns are missed today:
  - `sync_pr_task` may skip up-to-date PRs based on header `updatedAt` vs `last_synced_at`.
  - CI changes can happen without the PR becoming "changed enough" for timely discovery.
  - `sync_ci_for_shas` is targeted and effective, but currently scheduled by sweeps/planners rather than event push.
- Result:
  - stale queue gating state until manual resync or next fallback sweep.

## Proposed Design

### 1) Add a GitHub webhook ingress endpoint
- Add `POST /webhooks/github/` in app routing (`qb_site/qb_site/urls.py`), backed by a new `syncer` view module.
- Endpoint behavior:
  - `csrf_exempt`,
  - only accepts `POST`,
  - verifies HMAC signature using shared secret (`X-Hub-Signature-256` + request body),
  - reads `X-GitHub-Event` and `X-GitHub-Delivery`,
  - ACKs quickly (`202`) after enqueueing async work.
- Config:
  - `GITHUB_WEBHOOK_SECRET` (required when webhook endpoint is enabled),
  - optional `SYNCER_GITHUB_WEBHOOK_ENABLED` flag for staged rollout.

### 2) Delivery idempotency + auditing
- Add a small delivery ledger model in `syncer` (or `core`) keyed by delivery id:
  - fields: delivery id, event type, action, repo owner/name, received_at, processed_at, status, summary JSON.
- Rules:
  - first writer wins on delivery id,
  - duplicates return `202` without re-enqueueing,
  - invalid signatures are rejected with `403` and lightweight log context.
- Keep payload retention minimal (or none) to avoid unnecessary storage of raw webhook bodies.

### 3) Event router to existing task graph
- Route webhook events to current syncer tasks instead of building a second ingestion pipeline.
- Initial event coverage:
  - `pull_request`:
    - actions like `opened`, `reopened`, `synchronize`, `edited`, `ready_for_review`, `converted_to_draft`, `closed`.
    - enqueue `syncer.sync_pr` for the PR number (force false by default).
  - `check_run` and `check_suite`:
    - actions indicating CI lifecycle progress/completion and reruns.
    - extract `head_sha`.
    - enqueue `syncer.sync_ci_for_shas` for affected PR(s).
- PR fanout strategy for CI events:
  - primary: local DB match `PullRequest(repository, head_sha=payload_sha)` for open PRs.
  - fallback: if no local PR match but payload includes PR references, enqueue matching PR numbers.
  - safety fallback: if still unresolved, enqueue a bounded `sync_repo_since` for that repo to discover late-mapped PRs.

### 4) CI rerun correctness path (core requirement)
- For check events, prefer SHA-targeted refresh (`sync_ci_for_shas`) over PR bundle refresh.
- Rationale:
  - directly updates commit-scoped CI rows (`CommitCheckRun`, `CommitStatusContext`),
  - independent of PR `updatedAt`,
  - naturally handles repeated attempts for same context/sha.
- Follow-up trigger:
  - after successful CI-by-SHA updates, enqueue `analyzer.process_pr` for touched PR ids so queue windows/snapshots refresh without waiting for sweeps.

### 5) Poller/backfill remains authoritative fallback
- Keep current scheduled jobs unchanged:
  - `sync_active_repos`,
  - `backfill_repo_history_active`,
  - `backfill_repo_incomplete_prs_active`,
  - `refresh_pending_ci_for_active_repos`.
- Webhooks become fast-path signals.
- Pollers/backfills remain recovery path for:
  - webhook downtime,
  - dropped deliveries,
  - signature misconfigurations during rollout.

### 6) Concurrency, dedupe, and rate safety
- Reuse existing enqueue dedupe strategy (`030-sync-task-dedupe-strategy`) for webhook-originated enqueue points.
- Reuse CI SHA backoff policy for CI-by-SHA fanout where appropriate.
- Keep GitHub API calls in background tasks only; webhook handler should not call GitHub directly.

## Subtleties / Invariants
- Invariant: webhook ingress must be fast and side-effect minimal; heavy work is async only.
- Invariant: duplicate delivery ids must not create duplicate downstream fanout.
- Invariant: out-of-order check events must not regress stored CI state (existing upsert logic is latest-wins by timestamps/provider ids).
- Invariant: fallback pollers remain enabled until webhook reliability is proven in production.
- Invariant: repository activation gate applies; ignore webhook events for repos not configured as active in `core.Repository`.

## Data Model and API Changes (Planned)
- New model (name TBD): `GitHubWebhookDelivery`.
- New endpoint(s):
  - `POST /webhooks/github/`.
- New settings:
  - `GITHUB_WEBHOOK_SECRET`,
  - `SYNCER_GITHUB_WEBHOOK_ENABLED` (optional feature flag),
  - optional allowlist: `SYNCER_GITHUB_WEBHOOK_EVENTS`.

## Implementation Plan (Chunks)
1. Endpoint skeleton + signature verification.
2. Delivery ledger model + duplicate suppression.
3. Event parsing/routing service with structured result payload.
4. Pull request event fanout to `syncer.sync_pr`.
5. Check event fanout to `syncer.sync_ci_for_shas` with PR resolution by `(repo, head_sha)`.
6. Analyzer follow-up enqueue after webhook-triggered CI updates.
7. Observability: logs/counters/admin view for delivery outcomes and routing reasons.
8. Rollout controls: feature flag, dry-run mode (route-only logs, no enqueue), then enable enqueue.

### Implementation Status
- Chunk 1: implemented.
- Chunk 2: implemented.
- Chunk 3: implemented.
- Chunk 4: implemented.
- Chunk 5: implemented.
- Chunk 6+: pending.

## Validation Plan
- Unit tests:
  - signature validation accepts valid HMAC and rejects invalid/missing signature,
  - delivery id dedupe prevents duplicate fanout,
  - router maps each supported event/action to expected task requests,
  - CI event PR-resolution logic (head_sha match, payload PR fallback, repo-level fallback).
- Task-level tests:
  - webhook-triggered `sync_ci_for_shas` causes commit-scoped CI row updates,
  - analyzer follow-up is enqueued for touched PRs.
- Integration tests:
  - Django test client posts representative GitHub payload fixtures and asserts:
    - HTTP status,
    - delivery ledger row status,
    - expected Celery task enqueue calls.
- Manual ops checks:
  - temporarily rerun a failed check on a known PR and confirm CI status refresh without manual SHA resync.

## Rollout and Operations
- Phase 0: ship code dark (`SYNCER_GITHUB_WEBHOOK_ENABLED=false`), run tests.
- Phase 1: enable endpoint in dry-run routing mode in staging.
- Phase 2: enable enqueue for one repository; compare freshness vs current poll cadence.
- Phase 3: enable for all active repos; keep pollers unchanged.
- Phase 4: tune polling frequencies only after observed webhook stability and lag metrics.

## Operational Notes: GitHub App and Webhook Setup
- When to create/configure the GitHub App:
  - Create the app before Phase 1 (staging dry-run), after endpoint + signature verification code is merged and deployed.
  - Do not point production repos at the webhook until Phase 2 starts (single-repo canary with enqueue enabled).
- Recommended sequencing:
  1. Deploy code with webhook endpoint and `SYNCER_GITHUB_WEBHOOK_ENABLED=false`.
  2. Create GitHub App in staging, set webhook URL to staging endpoint, set webhook secret.
  3. Validate delivery signatures and routing logs in dry-run mode (no enqueue).
  4. Enable enqueue in staging.
  5. Create/update production GitHub App webhook URL + secret.
  6. Install app on one production repository (canary), monitor for 24-48 hours.
  7. Expand installation to remaining active repositories.
- Webhook URL targets:
  - Staging: `https://<staging-host>/webhooks/github/`
  - Production: `https://<prod-host>/webhooks/github/`
- Required app webhook event subscriptions (phase 1):
  - Pull requests,
  - Check runs,
  - Check suites.
- Secret and configuration handling:
  - Keep `GITHUB_WEBHOOK_SECRET` in deployment secret manager.
  - Rotate secret by supporting a short dual-secret overlap window during deployment, then remove old secret.
  - Keep `SYNCER_GITHUB_WEBHOOK_ENABLED` as a fast rollback toggle.
- Failure/rollback runbook:
  - If signature failures spike, disable webhook processing (`SYNCER_GITHUB_WEBHOOK_ENABLED=false`) and rely on pollers while fixing secret/config mismatch.
  - If enqueue volume spikes unexpectedly, keep endpoint enabled but switch to dry-run routing mode to inspect event mix.
  - If delivery backlog appears in GitHub, verify app installation scope and endpoint latency before reducing poll cadence.
- Post-cutover steady-state guidance:
  - Keep pollers/backfills enabled for at least one full CI retention/seasonality cycle before considering any schedule reductions.
  - Revisit polling intervals only after observing stable webhook delivery and reduced CI freshness lag.

## Observability Additions
- Delivery-level counters:
  - received,
  - invalid_signature,
  - duplicate_delivery,
  - routed_noop,
  - routed_enqueue_success,
  - routed_enqueue_error.
- Routing summaries:
  - event/action,
  - repo,
  - PRs resolved,
  - SHAs resolved,
  - tasks enqueued.
- Convergence watchpoints:
  - trend in `prs_missing_head_ci_contexts`,
  - lag between check completion event time and analyzer-visible CI state change.

## Open Questions
- Should we support `workflow_run` in phase 1, or keep to `pull_request` + `check_run` + `check_suite` initially?
- Do we need a separate dead-letter queue for malformed but signed payloads, or is structured logging sufficient initially?
- Should webhook events for unknown repositories auto-create `Repository` rows, or remain strict/no-op?

## Deferred Work
- Direct webhook payload application (selective write-through):
  - Current design treats webhook payloads as trigger/route signals and keeps canonical writes in sync tasks.
  - Future optimization: for specific actions with trustworthy fields, apply small direct updates (for example, narrow PR field updates or event markers) to reduce follow-up GraphQL reads.
  - Guardrails for this future work:
    - keep idempotency by delivery id + object identity,
    - avoid expanding payload handling into a second full ingestion path,
    - retain `sync_pr`/`sync_ci_for_shas` as canonical reconciliation path.

## Progress Notes
### Update Discipline
- Keep this section updated after each implemented chunk:
  - what changed,
  - tests added/updated,
  - subtleties discovered that affect next chunks.

### 2026-03-04 - Initial plan baseline
- Reviewed current syncer/analyzer codepaths and existing design docs.
- Confirmed no GitHub webhook endpoint existed yet.
- Confirmed CI-by-SHA and commit-scoped CI storage were already in place, so webhook work could focus on ingestion/routing/reliability.
- Drafted this living plan as the implementation baseline.

### 2026-03-06 - Chunk 1 (endpoint + signature verification)
- Implemented `POST /webhooks/github/` endpoint with:
  - feature flag gate (`SYNCER_GITHUB_WEBHOOK_ENABLED`),
  - secret-based HMAC verification (`GITHUB_WEBHOOK_SECRET`, `X-Hub-Signature-256`),
  - fast `202` acknowledge path.
- Added endpoint tests for method gating, disabled endpoint, missing secret, invalid signature, valid signature.
- Subtleties discovered:
  - In local/sandbox test environments, webhook endpoint tests should avoid DB dependence when possible (used `SimpleTestCase` for pure endpoint behavior in this chunk).

### 2026-03-06 - Chunk 2 (delivery ledger + duplicate suppression)
- Added `GitHubWebhookDelivery` model and migration for delivery-level idempotency/auditing.
- Webhook handler now requires `X-GitHub-Delivery`, records accepted deliveries, and returns `202 duplicate` on replay.
- Added tests for missing delivery id and duplicate delivery behavior.
- Subtleties discovered:
  - Delivery id is the primary idempotency key for webhook ingestion and should be treated as required for all production processing.

### 2026-03-06 - Docs/compatibility pass against GitHub webhook docs
- Verified behavior against GitHub docs for:
  - signature validation expectations,
  - delivery/event headers,
  - payload content-type variants.
- Added support for `application/x-www-form-urlencoded` webhook payloads (`payload=...` JSON) in addition to JSON bodies.
- Added tests for missing signature header and form-encoded payload parsing.
- Subtleties discovered:
  - Signature must be computed over raw bytes exactly as delivered; parsing mode can vary but verification input must not.

### 2026-03-06 - Chunk 3 (structured router service)
- Added `syncer.services.github_webhook_router.route_github_webhook(...)` returning normalized routing summary.
- Webhook handler now stores structured route summary in delivery ledger and logs route.
- Added router unit tests (`pull_request`, `check_run`, unsupported event).
- Subtleties discovered:
  - Centralized parsing/routing keeps endpoint and future fanout logic testable and avoids scattering event-shape conditionals.

### 2026-03-06 - Chunk 4 (pull_request fanout to sync_pr)
- Implemented webhook fanout for `pull_request` route:
  - repo identity match in local DB,
  - `is_active=True` gating,
  - enqueue `syncer.sync_pr` by `(repo_id, pr_number)`.
- Added endpoint tests for enqueue path and inactive/missing repo skip path.
- Subtleties discovered:
  - Current `pull_request` action filtering is intentionally broad (any parsed `pull_request` event can enqueue), prioritizing correctness over efficiency in early rollout.
  - Action-level filtering/allowlist is a near-term optimization for upcoming chunks.

### 2026-03-06 - Chunk 5 (check event fanout to sync_ci_for_shas + action filtering)
- Implemented webhook fanout for `check_run` / `check_suite` routes:
  - repo identity match in local DB with `is_active=True` gate,
  - `head_sha`-based PR resolution from local open PRs (`PullRequest.head_sha`) plus payload PR references,
  - enqueue `syncer.sync_ci_for_shas` per resolved PR with `shas=[head_sha]`.
- Added action filtering:
  - `pull_request` and `check_*` routes now ignore non-tracked actions (`reason=ignored_action`) and do not enqueue.
- Added endpoint tests for:
  - check event enqueue path,
  - check event ignored-action path,
  - pull_request ignored-action path.
- Subtleties discovered:
  - Payload PR references in check events are useful fallback data, but local `(repo, head_sha)` resolution remains the primary correctness source for active PRs.
  - Action allowlists materially reduce unnecessary enqueues while keeping recovery safety via periodic pollers/backfills.

## Finalization Notes
- After implementation stabilizes, convert this file into a concise final decision record:
  - `Context`,
  - `Decision`,
  - `Consequences`,
  - `Operational Notes`,
  - optional `Alternatives`.

## References
- `qb_site/qb_site/urls.py`
- `qb_site/syncer/tasks/sync_tasks.py`
- `qb_site/syncer/services/ci_by_sha_service.py`
- `qb_site/syncer/services/sub/ci_sync.py`
- `qb_site/syncer/models/commit_check_run.py`
- `qb_site/syncer/models/commit_status_context.py`
- `docs/design-decisions/029-updatedat-discovery-watermark-and-catchup.md`
- `docs/design-decisions/030-sync-task-dedupe-strategy.md`
- `docs/design-decisions/032-sha-keyed-ci-storage-migration-plan.md`
