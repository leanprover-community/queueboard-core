# Syncer Ingestion Plan (v1)

This document outlines the v1 GitHub ingestion architecture that powers the raw data tables in `syncer`. It optimizes for:
- Parity with the current queueboard (labels + CI + key timeline events)
- Low request volume (one GraphQL “PR bundle” per changed PR)
- Idempotent, testable modules
- Clean separation from Analyzer (derived CI transitions, status evolution)

See also: `docs/django_backend_plan.md` for the broader migration plan and model summaries. CI signal choices and tradeoffs are captured in `docs/design-decisions/004-ci-status-sources.md`.
Pagination strategy for timeline and commits is captured in `docs/design-decisions/005-page-until-cutoff-pagination.md`.

## Goals
- Persist raw facts for open PRs so we can reproduce dashboards and compute analytics:
  - PullRequest core fields
  - Current labels (LabelDef + PRLabel)
  - Key timeline events (label add/remove; draft toggles; reopened/closed)
  - CI history from CheckRun snapshots and StatusContext snapshots (latest per commit context via statusCheckRollup)
- Minimize API calls while staying robust and easy to reason about.

## Discovery & Preflight
- Discovery (incremental): `GitHubClient.get_changed_pr_numbers(owner, name, since_iso, states=[OPEN], limit=N)` pages the PR list ordered by `UPDATED_AT` and stops at the cutoff or limit.
- Preflight (per PR): `GitHubClient.get_pr_header(owner, name, number)` fetches `updatedAt`; skip ingestion when `updatedAt <= PullRequest.last_synced_at`.
- Commands:
  - `list_changed_prs` lists PR numbers for manual testing.
  - `sync_repo --since` discovers changed PRs, preflights each PR, then ingests with the bundle.

## Data Sources: Single “PR Bundle” Query
- One GraphQL request per changed PR fetches all needed data:
  - pullRequest core: number/author/state/isDraft/title/body/createdAt/updatedAt/baseRefName/headRefName/headRepo owner+name/additions/deletions/changedFiles
  - labels: `labels(first: 100) { nodes { name color } }`
  - timelineItems: `first: K` and filtered to `[LABELED_EVENT, UNLABELED_EVENT, READY_FOR_REVIEW_EVENT, CONVERT_TO_DRAFT_EVENT, REOPENED_EVENT, CLOSED_EVENT]`
  - commits: `last: M` (head commit window), with:
    - status.contexts { id, __typename, context/state OR name/status/conclusion, targetUrl/detailsUrl, createdAt/startedAt/completedAt }
      (latest per context per commit; union of `StatusContext` and `CheckRun`)
  - Both connections include `pageInfo` (timeline: `hasNextPage`/`endCursor`; commits: `hasPreviousPage`/`startCursor`).
  - All queries include a `rateLimit { cost remaining resetAt used }` snapshot for budgeting/logging.
- Tunable limits (defaults; adjust per repo in settings):
  - `K` (timeline items): ~150–200
  - `M` (commits): ~10–20
- Optional batching: alias multiple PR bundles in one GraphQL call once stable.

## Models and Ownership
- Syncer (raw facts):
  - `PullRequest`, `LabelDef`, `PRLabel`, `PRTimelineEvent`, `CheckRun` (snapshot via statusCheckRollup), `StatusContext` (snapshot via statusCheckRollup)
  - Core entity touch points: update `core.Repository.github_node_id` and `core.Repository.default_branch`; upsert `core.User` for PR authors (and later reviewers/assignees) using GitHub node id or login.
- Analyzer (derived semantics):
  - `PRCIStatusEvent` (coarse CI transitions: pass/fail/fail-inessential/running/missing) computed from CheckRun + StatusContext and the repo-configurable “inessential jobs” set.

## Ingestion Flow (Per Repository)
1) Discover changed PRs
- GraphQL search for open PRs, ordered by `updatedAt`, gated by `last_synced_at` (or a moving window).

2) Fetch bundle per PR (changed only)
- Execute the PR bundle; cap using K/M.

3) Persist in a single transaction per PR
- Repository: update `github_node_id` and `default_branch` if present in the bundle (`defaultBranchRef.name`).
- PullRequest: upsert by `(repository, number)` from bundle core; set `last_synced_at`.
- Author User: upsert in `core.User` (prefer `id` match, fallback to case-insensitive `login`; update `name`/`avatarUrl`).
- LabelDef: upsert by `(repository, lower(name))` with display `name` and `color`.
- PRLabel: diff the current attachments vs DB; bulk create missing and delete extras.
- PRTimelineEvent: insert key events by `github_node_id` with `occurred_at`; store `label_name` as-is.
- CheckRun (snapshot): from `status.contexts` union when `__typename == CheckRun`; upsert by `github_node_id`; set `head_sha`, `gh_started_at`, `gh_completed_at`.
- StatusContext (snapshot): from `status.contexts` union when `__typename == StatusContext`; upsert by `github_node_id`; set `head_sha`, `gh_created_at`.

4) Trigger Analyzer
- Enqueue recompute for `PRCIStatusEvent` (and downstream status evolution) for changed PRs.

## Idempotency Keys and Indexes
- PullRequest: unique `(repository, number)`
- LabelDef: unique `(repository, lower(name))`
- PRLabel: unique `(pull_request, label_def)`
- PRTimelineEvent: conditional unique on `github_node_id` when present
- CheckRun: unique `github_node_id`
- StatusContext: unique `github_node_id`
- Replay indexes: `(pull_request, occurred_at)` for timeline; `(pull_request, gh_completed_at)` (CheckRun) and `(pull_request, gh_created_at)` (StatusContext)

## Paging Queries (v1)
- Keep the bundle for the first page; add lean page queries for subsequent pages when needed:
  - `queries/timeline_page.graphql`: pages `timelineItems(first:$first, after:$after)` and returns nodes + pageInfo.
  - `queries/commits_page.graphql`: pages `commits(last:$last, before:$before)` and returns commit oids + contexts + pageInfo.
- Orchestrator can fetch additional pages with small caps; cutoffs are described in `005-page-until-cutoff-pagination.md` and can be added after the first iteration.

## Services & File Organization
- `syncer/services/github_client.py`
  - `GitHubClient.execute(...)`
  - `GitHubClient.get_changed_pr_numbers(owner, name, since)`
  - `GitHubClient.get_pr_bundle(owner, name, number, limits)`
- `syncer/queries/pr_bundle.graphql` (single source of truth for the bundle)
- `syncer/services/pr_sync_service.py`
  - `PRSyncService.sync_repository(repo, since, limits)`
  - `PRSyncService.sync_pull_request(repo, number, limits)`
- Sub‑syncs (pure mapping + DB writes):
  - `services/sub/core_entities_sync.py`: `upsert_repo_metadata`, `upsert_repo_node_id`, `upsert_user_from_github`
  - `services/sub/pull_request_sync.py`: `upsert_pull_request(...)`
  - `services/sub/labels_sync.py`: `sync_label_catalog(...)`, `sync_pr_labels(...)`
  - `services/sub/timeline_sync.py`: `sync_timeline_events(...)`
  - `services/sub/ci_sync.py`: `sync_check_runs(...)` (snapshots) and `sync_status_contexts(...)` (snapshots)
- Tasks & CLI:
  - `syncer/tasks/sync_tasks.py`: `sync_repo_task`, `sync_pr_task`
  - `syncer/management/commands/sync_repo.py`: repo runner with `--since` and bundle limits

## Incremental & Backfills
- Incremental discovery
  - Use a lightweight listing on `repository.pullRequests` ordered by `UPDATED_AT DESC` to enumerate candidate PR numbers since a cutoff. Stop paging when `updatedAt < since`.
  - Method: `GitHubClient.get_changed_pr_numbers(owner, name, since_iso, states=[OPEN], limit=N)`
  - Typical usage: `states=[OPEN]` for ongoing sync; broaden to `MERGED,CLOSED` when backfilling.
- Ingestion gating
  - Before fetching a bundle, optionally compare the PR's GraphQL `updatedAt` to our `PullRequest.last_synced_at` and skip when unchanged.
- Backfill phases (v1)
  - Phase A: all OPEN PRs + CLOSED/MERGED from the past ~90 days for dashboard parity.
  - Phase B: extend the window to 6–12 months if historical analytics requires it.
  - Phase C (optional later): CI history backfill via REST if we decide to track multiple transitions per commit SHA.
- Resume & idempotency
  - Keep a small JSON resume file (per repo) storing pagination cursor and counters, or later add a `SyncJob` table to persist job metadata.
  - Ingestion is idempotent by design (unique constraints on PR identity, label defs, attachments, timeline event ids, and CI snapshot ids).

## Rate Limits & Logging
- Every query selects `rateLimit { cost remaining resetAt used }`; `GitHubClient` caches the last snapshot.
- `sync_repo` prints:
  - rateLimit.remaining and resetAt early in the run (piggy-backing on the first API call),
  - per-query cost/remaining for header, bundle, and any page queries,
  - resetAt again only if it changes,
  - final remaining at the end of the run.
- Budgeting strategy: stop when remaining drops near a threshold and schedule a continuation at `resetAt` (see `django_backend_plan.md` for scheduling roadmap).

## Rate/Cost Controls
- Keep K/M bounded; log GraphQL costs and durations.
- Coalesce bursts per PR (small debounce); apply a per‑PR lock to avoid concurrent syncs.
- Batch multiple PR bundles via GraphQL aliases when safe.

## Webhooks (Later)
- Add `/webhooks/github` to enqueue `sync_pr_task` on PR events; write `CheckRun`/`StatusContext` directly by their IDs from payloads.
- Keep poller as safety net and for backfills.

## Testing
- Service unit tests with bundle fixtures (idempotency, label diff, timeline ignore_conflicts, CI upserts).
- Orchestrator tests (`sync_repository`) for changed vs unchanged PRs.
- Integration: parity checks vs legacy JSON outputs on a sample corpus.

## Configuration
- `GH_TOKEN` env var; timeouts/retries in the client.
- Defaults for `SYNCER_BUNDLE_LIMITS` (K/M) in Django settings with per‑repo override possible.
- Optional: analyzer “inessential jobs” list in settings (per repo).

## Future Extensions
- Add `PRAssignee`, `Review`, `Comment`, `PRDependency`, and (if needed) `Commit` tables.
- Add Snapshot materializations in Analyzer (e.g., queue snapshot) for fast UI reads.
- Status history: enable append‑only StatusContext history via REST (`/commits/{sha}/statuses`) and/or CheckRun attempt history via `checkSuites → checkRuns`. Add conditional upserts keyed by `rest_id` (StatusContext) and extend replay to subtract fail/running intervals on a single SHA.

## Implementation Status (v1)
- Queries
  - `qb_site/syncer/queries/pr_bundle.graphql` (variables: `$owner`, `$name`, `$number`, `$timelineK`, `$commitsM`); includes `pageInfo` and `rateLimit`.
  - `qb_site/syncer/queries/timeline_page.graphql` and `commits_page.graphql` for optional extra pages (lean connections only); include `rateLimit`.
  - `qb_site/syncer/queries/pr_header.graphql` for preflight (`updatedAt`, state, draft) with `rateLimit`.
- Services (sub‑syncs)
  - PR core: `syncer/services/sub/pull_request_sync.py` (`upsert_pull_request`)
  - Labels: `syncer/services/sub/labels_sync.py` (`sync_label_catalog`, `sync_pr_labels`)
  - Timeline: `syncer/services/sub/timeline_sync.py` (`sync_timeline_events`)
  - CI snapshots: `syncer/services/sub/ci_sync.py` (`sync_check_runs`, `sync_status_contexts`)
- CLI commands
  - File‑based: `qb_site/manage.py sync_pr_from_file --repo OWNER/NAME --file PATH [--dry-run]` — ingest a saved bundle.
  - Discovery: `qb_site/manage.py list_changed_prs --repo OWNER/NAME --since ISO [--states OPEN --limit N]` — list PR numbers since cutoff.
  - Live ingest: `qb_site/manage.py sync_repo --repo OWNER/NAME [--since ISO --limit N] [--number N ...]` — preflights and ingests changed PRs; prints `rateLimit` budget and per‑query costs.
  - Optional paging: `PRSyncService.sync_pull_request` supports page caps for timeline/commits; cutoff‑based loops planned.
- Tests
  - Sub‑sync unit tests and a command integration test under `qb_site/syncer/tests/` with a minimal fixture at
    `qb_site/syncer/tests/fixtures/pr_bundle_min.json`.
  - Run inside compose: `docker compose exec -T web python qb_site/manage.py test syncer`.

- Admin
  - Read‑only registrations for `PullRequest`, `LabelDef`, `PRLabel`, `PRTimelineEvent`, `CheckRun`, `StatusContext`.
  - PullRequest actions: enqueue sync (real or dry‑run) — lists task IDs after submission.
  - Repository “Sync tools” page: sync specific PR numbers or discover+sync since cutoff (real or dry‑run), with simple result table.

- Tasks/results
  - Celery task: `syncer.sync_pr` (sync_pr_task) — preflight + PR ingest with rateLimit logging; returns a summary dict.
  - `django-celery-results` enabled (when `CELERY_RESULT_BACKEND=django-db`) shows task status/name/result in admin.

Notes
- Snapshots only: `statusCheckRollup` returns the latest state per context on each commit. Timeline and CI commit windows are capped by `$timelineK` and `$commitsM`.

## Next Steps
- Page‑until‑cutoff loops
  - Use cutoff (`last_synced_at` or `--since`) to stop paging as soon as we cross the boundary (with small page caps for safety).
  - Log page counts and truncation flags for tuning.
- Repo‑level task + scheduling
  - Implement `sync_repo_since_task(repo_id, since, limit, states)` with per‑repo lock and rate‑budget stop; enqueue continuation at `resetAt`.
  - Add a lightweight “kick all repos” beat schedule and global single‑token guard if needed.
- Admin polish
  - Link enqueued Task IDs to Task Results; show recent results on the repo tools page.
  - Optional: add a “preflight only” report (no enqueue) summarizing updatedAt vs last_synced_at.
- Tests
  - Orchestrator paging tests; repo‑task budget/continuation tests (once added).
  - Admin view/form tests (basic GET/POST flows).

### Generating a Bundle (dev)
- Authenticate gh: `gh auth status` (or set `GH_TOKEN`).
- Run the bundle query for a single PR and write to JSON:
  ```bash
  gh api graphql \
    -F query=@qb_site/syncer/queries/pr_bundle.graphql \
    -F owner='leanprover-community' -F name='mathlib4' \
    -F number=30723 -F timelineK=150 -F commitsM=15 \
    > pr-30723.json
  ```
  If your shell/gh doesn’t expand `@file`, use:
  ```bash
  gh api graphql \
    -F query="$(< qb_site/syncer/queries/pr_bundle.graphql)" \
    -F owner='leanprover-community' -F name='mathlib4' \
    -F number=30723 -F timelineK=150 -F commitsM=15 \
    > pr-30723.json
  ```
- Sanity‑check shape with `jq`:
  - PR core: `jq '.data.repository.pullRequest.number' pr-30723.json`
  - Timeline types: `jq -r '.data.repository.pullRequest.timelineItems.nodes[].__typename' pr-30723.json | sort | uniq -c`
  - Commit oids: `jq -r '.data.repository.pullRequest.commits.nodes[].commit.oid' pr-30723.json`
  - Context types per commit: `jq -r '.data.repository.pullRequest.commits.nodes[].commit.statusCheckRollup.contexts.nodes[].__typename' pr-30723.json | sort | uniq -c`
