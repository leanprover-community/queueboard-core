# CI by SHA backoff ledger and SHA-keyed CI migration

## Context
- CI contexts are fetched per commit SHA (GraphQL statusCheckRollup), but we currently store them per PR in `syncer.CheckRun` and `syncer.StatusContext`.
- Some PRs can end up with `head_ci_state=SUCCESS` while the required contexts for the head SHA are missing in the DB.
- We already have multiple CI refresh pathways:
  - `syncer.refresh_pending_ci_for_repo` (pending contexts only),
  - `analyzer.plan_missing_ci` (revision-based missing/pending CI),
  - commit-history harvest backfills (SHA-based).
- GitHub CI logs and context history expire (often around a year). When a SHA is too old, repeated CI-by-SHA requests are wasted.
- Recent work added a backstop in `refresh_pending_ci_for_repo` to enqueue CI-by-SHA when the head SHA has no stored contexts. This solves the immediate bug but can still repeatedly hit expired SHAs.
- Longer term, CI is fundamentally commit-scoped; PR-scoped storage creates ambiguity when SHAs are shared across PRs or force-pushes.

This decision has two parts:
1) Add a CI-by-SHA backoff ledger to prevent repeated fetches of expired/missing CI.
2) Plan a migration to SHA-keyed CI storage while keeping current behavior stable.

## Decision (Part 1: CI-by-SHA backoff ledger)
- Introduce a persistent, per-repository SHA ledger that records CI-by-SHA fetch attempts and results.
- Gate all CI-by-SHA enqueue paths on this ledger to avoid repeated requests for SHAs that are known to be empty/expired.
- Keep the ledger agnostic to PRs; it should be keyed by repository + SHA.

### Proposed data model
- New model: `syncer.CIShaFetchState` (in `qb_site/syncer/models/`):
  - `repository` (FK)
  - `sha` (char(64), indexed)
  - `last_attempted_at` (DateTime)
  - `last_success_at` (DateTime, nullable)
  - `last_result` (enum/string: `ok`, `empty`, `not_found`, `error`, `skipped_association`, `filtered`)
  - `attempts` (int)
- Unique constraint on `(repository, sha)`.
- Backoff windows are derived from settings rather than stored in the row.

### Enqueue policy
- Add a helper (`syncer.services.ci_backoff.should_enqueue_ci_sha(...)`) that:
  - Allows enqueue if there is no ledger row.
  - Skips enqueue when `last_result` indicates stale data and the cooldown has not elapsed.
  - Treats `skipped_association` as a no-op result (no backoff); this should be deprecated once CI is SHA-keyed.
- Default cooldowns (configurable settings):
  - `SYNCER_CI_SHA_BACKOFF_EMPTY_SECONDS` (default 300)
  - `SYNCER_CI_SHA_BACKOFF_ERROR_SECONDS` (default 300)
- `not_found` and `filtered` are treated as infinite cooldowns (no retries).

### Write policy
- Update ledger in the CI-by-SHA ingest path:
  - `syncer.services.ci_by_sha_service.sync_ci_for_sha` returns a result classification.
  - `syncer.services.ci_backoff.record_ci_sha_fetch` persists attempts after each SHA fetch.
  - Result mapping:
    - `ok`: contexts found (regardless of allowlist filtering)
    - `empty`: commit exists but `statusCheckRollup` is null or contexts list empty
    - `not_found`: commit object missing in all candidate repos
    - `skipped_association`: association guard failed (when enabled)
    - `filtered`: contexts returned but all filtered out by allowlists
    - `error`: request failed (exceptions)

### Integration points
- Apply the guard before enqueueing CI-by-SHA in:
  - `syncer.refresh_pending_ci_for_repo_task` (head-missing backstop path),
  - `analyzer.plan_missing_ci` (revision-based backfill),
  - `syncer.harvest_commit_history_task` (commit-history missing/pending),
  - admin or manual enqueue tools if they go through shared helpers.
- Expose counts in task results:
  - `shas_skipped_backoff`, `prs_skipped_backoff` (refresh task)
  - `ci_shas_skipped_backoff` (commit history task)
  - `prs_skipped_backoff` (analyzer backfill task)

### Tests
- Unit tests for the ledger policy (no row, empty cooldown, error cooldown, not_found infinite).
- Integration tests that show:
  - Missing-head backstop skips when ledger says `empty` and cooldown not elapsed.
  - A changed SHA bypasses old backoff (new key).

## Decision (Part 2: Migration to SHA-keyed CI storage)
- Add SHA-keyed CI tables and transition the analyzer to read by SHA with PR fallbacks.
- Keep PR-keyed tables during migration to avoid destabilizing current snapshot behavior.

### Goals
- CI data is commit-scoped, so it should be stored and queried by `(repo, head_sha)`.
- Avoid per-PR duplication and ambiguity during rebases or when a SHA belongs to multiple PRs.
- Enable CI reuse across PRs and improve correctness for force-push timelines.

### Proposed schema additions
- New tables in `syncer/models/` (names TBD):
  - `CommitCheckRun`:
    - `repository`, `head_sha`, `name`, `status`, `conclusion`, `provider_id`, timestamps, `last_synced_at`.
  - `CommitStatusContext`:
    - `repository`, `head_sha`, `name`, `state`, `provider_id`/`rest_id`, timestamps, `last_synced_at`.
- Unique constraints on provider IDs and `(repository, head_sha, name, provider_id)` as applicable.
- Index `(repository, head_sha)` for fast lookup.

### Ingest strategy (dual-write)
- Update ingest paths to write to SHA-keyed tables first, and optionally also to PR-keyed tables:
  - `syncer.services.pr_sync_service` (bundle ingest)
  - `syncer.services.ci_by_sha_service` (CI-by-SHA)
- Retain PR-keyed tables during transition to minimize changes to existing code.

### Read strategy (dual-read with fallback)
- Update Analyzer CI helpers to read SHA-keyed tables by default:
  - `analyzer.services.queue_windows._latest_ci_statuses_for_prefix`
  - `analyzer.services.queueboard_snapshot._ci_status_for_pr`
- If SHA-keyed rows are missing, optionally fall back to PR-keyed rows for a transitional period.
- Ensure all required contexts are evaluated against the head SHA for that time window (PRRevision + head SHA).

### Backfill plan
- Background backfill to populate SHA-keyed tables from PR-keyed rows:
  - For each `CheckRun` / `StatusContext`, insert into SHA-keyed table keyed by `(repo, head_sha)`.
  - De-duplicate by provider IDs and names.
- Sequence:
  1) Create new tables and indices.
  2) Dual-write on all new ingests.
  3) Backfill from existing PR-keyed tables.
  4) Switch Analyzer reads to SHA-keyed tables (with fallback to PR-keyed).
  5) After validation, remove fallback and stop writing PR-keyed rows.
  6) Optionally drop PR-keyed tables after a deprecation period.

### Relationship to PRRevision
- PRRevision already provides PR-to-SHA windows; it becomes the primary link between PRs and SHA-keyed CI.
- This aligns with `docs/design-decisions/012-prrevision-head-changes.md` and `013-prrevision-incremental-build-state.md`.

## Consequences
- Part 1 (ledger) prevents repeated wasted CI-by-SHA requests for expired data and stabilizes the new head-missing backstop.
- Part 2 (SHA-keyed CI) simplifies correctness for force-pushes and reduces duplicated CI storage.
- Dual-write and dual-read temporarily increase complexity but allow safe rollout.
- Some tasks will need updated metrics and tests to account for the ledger and new tables.

## Operational Notes
- Add new settings for CI-by-SHA backoff cooldowns.
- Add a new migration for `CIShaFetchState` (and later for SHA-keyed CI tables).
- Extend admin/convergence metrics to monitor:
  - `prs_missing_head_ci_contexts` (open PRs with missing head contexts),
  - `shas_skipped_backoff` / `prs_skipped_backoff`,
  - ledger row counts per repo.
- For Part 2, perform dual-write before switching reads.

## Alternatives
- Keep PR-keyed storage only and add ad-hoc guards (per-PR cool-downs).
  - Rejected: does not address SHA-level dedupe, still ambiguous across PRs.
- Use a transient cache (Redis) for backoff.
  - Rejected: loses history across restarts and is harder to audit.
