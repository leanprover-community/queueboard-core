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

## Progress Notes
- 2026-03-04:
  - Reviewed current syncer/analyzer codepaths and existing design docs.
  - Confirmed no GitHub webhook endpoint exists yet.
  - Confirmed CI-by-SHA and commit-scoped CI storage are already in place, so webhook work can focus on ingestion/routing/reliability.
  - Drafted this living plan as the implementation baseline.

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
