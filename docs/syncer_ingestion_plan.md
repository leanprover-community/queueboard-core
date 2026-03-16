# Syncer Ingestion Plan

This document outlines the v1 GitHub ingestion architecture that powers the raw data tables in `syncer`. It optimizes for:
- Parity with the current queueboard (labels + CI + key timeline events)
- Low request volume (one GraphQL “PR bundle” per changed PR)
- Idempotent, testable modules
- Clean separation from Analyzer (derived CI transitions, status evolution)

See also: `docs/django_backend_plan.md` for the broader migration plan and model summaries. CI signal choices and tradeoffs are captured in `docs/design-decisions/004-ci-status-sources.md`.
Pagination strategy for timeline and commits is captured in `docs/design-decisions/005-page-until-cutoff-pagination.md`.
Our watermark choice (single `last_synced_at` for V1) is recorded in `docs/design-decisions/006-pr-watermarks-single-vs-multiple.md`.

## Goals
- Persist raw facts for open PRs so we can reproduce dashboards and compute analytics:
  - PullRequest core fields
  - Current labels (LabelDef + PRLabel)
  - Key timeline events (label add/remove; draft toggles; reopened/closed)
  - CI history from commit-scoped CheckRun/StatusContext snapshots (latest per commit context via statusCheckRollup)
- Minimize API calls while staying robust and easy to reason about.

## Discovery & Preflight
- Discovery (incremental): `GitHubClient.get_changed_pr_numbers(owner, name, since_iso, states=[OPEN,MERGED,CLOSED], limit=N)` pages the PR list ordered by `UPDATED_AT` and stops at the cutoff or limit. The default discovery states are `OPEN,MERGED,CLOSED` (via `SYNCER_DISCOVERY_STATES_DEFAULT`); you can narrow this (e.g., to `OPEN` only) via settings or per-call overrides when needed.
- Preflight (per PR): `GitHubClient.get_pr_header(owner, name, number)` fetches `updatedAt`; skip ingestion when `updatedAt <= PullRequest.last_synced_at`.
- Commands:
  - `list_changed_prs` lists PR numbers for manual testing.
  - `sync_repo --since` discovers changed PRs, preflights each PR, then ingests with the bundle.

## Data Sources: Single “PR Bundle” Query
- One GraphQL request per changed PR fetches all needed data:
  - pullRequest core: number/author/state/isDraft/title/body/createdAt/updatedAt/baseRefName/headRefName/headRepo owner+name/additions/deletions/changedFiles
  - labels: `labels(first: 100) { nodes { name color } }`
  - timelineItems: `first: K`, `since: $timelineSince` (optional) and filtered to `[LABELED_EVENT, UNLABELED_EVENT, READY_FOR_REVIEW_EVENT, CONVERT_TO_DRAFT_EVENT, REOPENED_EVENT, CLOSED_EVENT, HEAD_REF_FORCE_PUSHED_EVENT]`
  - commits: `last: M` (head commit window), with:
    - status.contexts { id, __typename, context/state OR name/status/conclusion, targetUrl/detailsUrl, createdAt/startedAt/completedAt }
      (latest per context per commit; union of `StatusContext` and `CheckRun`)
    - commit metadata: `oid` (we avoid time-based paging on commits; see Historical Consistency below)
  - Both connections include `pageInfo` (timeline: `hasNextPage`/`endCursor`; commits: `hasPreviousPage`/`startCursor`).
  - All queries include a `rateLimit { cost remaining resetAt used }` snapshot for budgeting/logging.
- Tunable limits (defaults; adjust per repo in settings):
  - `K` (timeline items): ~150–200
  - `M` (commits): ~10–20
- Optional batching: alias multiple PR bundles in one GraphQL call once stable.

## Models and Ownership
- Syncer (raw facts):
  - `PullRequest`, `LabelDef`, `PRLabel`, `PRTimelineEvent`, `CommitCheckRun` (snapshot via statusCheckRollup), `CommitStatusContext` (snapshot via statusCheckRollup)
  - Core entity touch points: update `core.Repository.github_node_id` and `core.Repository.default_branch`; upsert `core.User` for PR authors (and later reviewers/assignees) using GitHub node id or login.
- Analyzer (derived semantics):
  - `PRCIStatusEvent` (coarse CI transitions: pass/fail/fail-inessential/running/missing) computed from commit-scoped CI rows and the repo-configurable “inessential jobs” set.

## Ingestion Flow (Per Repository)
1) Discover changed PRs
- GraphQL search for recently updated PRs (by default in states `OPEN,MERGED,CLOSED`), ordered by `updatedAt`, gated by a moving cutoff.

2) Fetch bundle per PR (changed only)
- Execute the PR bundle; cap using K/M.

3) Persist in a single transaction per PR
- Repository: update `github_node_id` and `default_branch` if present in the bundle (`defaultBranchRef.name`).
- PullRequest: upsert by `(repository, number)` from bundle core; set `last_synced_at`.
- Author User: upsert in `core.User` (prefer `id` match, fallback to case-insensitive `login`; update `name`/`avatarUrl`).
- LabelDef: upsert by `(repository, lower(name))` with display `name` and `color`.
- PRLabel: diff the current attachments vs DB; bulk create missing and delete extras.
- PRTimelineEvent: insert key events by `github_node_id` with `occurred_at`; store `label_name` as-is; persist `before_sha`/`after_sha` for force‑push events.
- CommitCheckRun (snapshot): from `status.contexts` union when `__typename == CheckRun`; upsert by `github_node_id`; set `head_sha`, `gh_started_at`, `gh_completed_at`.
- CommitStatusContext (snapshot): from `status.contexts` union when `__typename == StatusContext`; upsert by `github_node_id`; set `head_sha`, `gh_created_at`.

4) Trigger Analyzer
- Enqueue recompute for `PRCIStatusEvent` (and downstream status evolution) for changed PRs.

## Idempotency Keys and Indexes
- PullRequest: unique `(repository, number)`
- LabelDef: unique `(repository, lower(name))`
- PRLabel: unique `(pull_request, label_def)`
- PRTimelineEvent: conditional unique on `github_node_id` when present
- CommitCheckRun: unique `github_node_id`
- CommitStatusContext: unique `github_node_id`
- Replay indexes: `(pull_request, occurred_at)` for timeline; repository/SHA indexes for commit-scoped CI tables.

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
  - `GitHubClient.get_prs_created_page(owner, name, first, after, states)` for historical PR backfill ordered by `CREATED_AT ASC`.
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
    - `refresh_pending_ci_for_repo_task(repo_id, max_prs, max_shas_per_pr, max_pending_hours)` to re-poll CI for SHAs whose commit-scoped CI rows remain pending.
    - `refresh_pending_ci_for_active_repos_task(max_prs_per_repo, max_shas_per_pr, max_pending_hours)` to enqueue pending-CI refresh for all active repositories (used by Celery beat).
  - `syncer/tasks/backfill_tasks.py`:
    - `backfill_repo_history_task(repo_id, page_size, max_pages, states)` for createdAt-based history backfill.
    - `backfill_repo_history_active_task()` to enqueue history backfill for all active repositories (used by Celery beat).
    - `backfill_repo_incomplete_prs_task(repo_id, limit, states)` for incomplete-PR backfill (timeline/commits not yet marked done).
    - `backfill_repo_incomplete_prs_active_task(limit, states)` to enqueue incomplete-PR backfill for all active repositories (used by Celery beat).
  - `syncer/management/commands/sync_repo.py`: repo runner with `--since` and bundle limits
  - `syncer/management/commands/backfill_repo_history.py`: repo-level history backfill runner (sync or `--async` Celery enqueue)
  - `syncer/management/commands/backfill_incomplete_prs.py`: repo-level incomplete-PR backfill runner (sync or `--async` Celery enqueue)
  - `syncer/management/commands/refresh_pending_ci.py`: repo-level pending-CI refresh runner (sync or `--async` Celery enqueue)

## Incremental & Backfills
- Incremental discovery
  - Use a lightweight listing on `repository.pullRequests` ordered by `UPDATED_AT DESC` to enumerate candidate PR numbers since a cutoff. Stop paging when `updatedAt < since`.
  - Method: `GitHubClient.get_changed_pr_numbers(owner, name, since_iso, states=[OPEN,MERGED,CLOSED], limit=N)`.
  - Typical usage: default `states` come from `SYNCER_DISCOVERY_STATES_DEFAULT` (`OPEN,MERGED,CLOSED` by default); narrow to `OPEN` or broaden further via settings or per-call overrides when needed.
- Ingestion gating
  - Before fetching a bundle, optionally compare the PR's GraphQL `updatedAt` to our `PullRequest.last_synced_at` and skip when unchanged.
- Historical PR backfill (createdAt-based, v1.1)
  - Use `get_prs_created_page(owner, name, first, after, states=[OPEN,MERGED,CLOSED])` ordered by `CREATED_AT ASC` to discover PRs that may never have been synced.
  - Store a per-repo cursor in `syncer.RepoBackfillCursor` (`created_cursor`, `oldest_created_at`, `completed`, `last_run_at`) to resume between runs.
  - Task: `backfill_repo_history_task(repo_id, page_size, max_pages, states)`:
    - Pages PRs by createdAt starting from `created_cursor`,
    - Enqueues `sync_pr_task` for each discovered PR number,
    - Treats `completed` as “no more PRs as of last run” but continues to follow newly created PRs on subsequent runs.
  - This complements incremental discovery:
    - Ensures every PR is eventually synced at least once, even if created before the current discovery lookback window.
    - Sliding updatedAt-based discovery (`sync_repo_since`) keeps recently changed PRs fresh once they exist in the DB.
- Incomplete PR backfill (DB-based, v1.2)
  - Use the existing `PullRequest` table to find PRs whose backfill flags are still incomplete:
    - `timeline_backfill_done == False` or `commits_backfill_done == False`.
  - Task: `backfill_repo_incomplete_prs_task(repo_id, limit, states)`:
    - Filters by repository and optional GitHub-style states list (`OPEN`, `MERGED`, `CLOSED`, mapped onto the local `state` field).
    - Orders candidates by `gh_updated_at DESC, id DESC` and enqueues up to `limit` `sync_pr_task` runs per repository.
    - Uses `SYNCER_TIMELINE_BACKFILL_PAGES` / `SYNCER_COMMITS_BACKFILL_PAGES` as the page budgets passed through to `sync_pr_task`.
  - Coordinator: `backfill_repo_incomplete_prs_active_task(limit, states)`:
    - Iterates active repositories and enqueues a small slice of incomplete-PR backfill for each (periodically driven by Celery beat).
  - This complements createdAt-based history backfill:
    - History backfill ensures that every PR number is seen and synced at least once.
    - Incomplete-PR backfill gradually drives existing PRs to `timeline_backfill_done == True` and `commits_backfill_done == True`, even if they fall outside the discovery lookback window or were only partially backfilled before a low-budget run or downtime.
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
  - `qb_site/syncer/queries/pr_bundle.graphql` (variables: `$owner`, `$name`, `$number`, `$timelineK`, `$commitsM`, `$timelineSince?`); includes `pageInfo` and `rateLimit`.
  - `qb_site/syncer/queries/timeline_page.graphql` and `commits_page.graphql` for optional extra pages (lean connections only); `timeline_page` accepts `$since?`; both include `rateLimit`.
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
- Optional paging: `PRSyncService.sync_pull_request` supports page caps for timeline/commits; timeline uses `since = last_synced_at − epsilon` and pages forward; commit paging is a fixed window (last M) with optional capped extra pages — we do not rely on commit timestamps for cutoffs in V1.
- Tests
  - Sub‑sync unit tests and a command integration test under `qb_site/syncer/tests/` with a minimal fixture at
    `qb_site/syncer/tests/fixtures/pr_bundle_min.json`.
  - Run inside compose: `docker compose exec -T web python qb_site/manage.py test syncer`.

- Admin
  - Read‑only registrations for `PullRequest`, `LabelDef`, `PRLabel`, `PRTimelineEvent`, `CommitCheckRun`, `CommitStatusContext`.
  - PullRequest actions: enqueue sync (real or dry‑run) — lists task IDs after submission.
  - Repository “Sync tools” page: sync specific PR numbers or discover+sync since cutoff (real or dry‑run), with simple result table.

- Tasks/results
- Celery task: `syncer.sync_pr` (sync_pr_task) — preflight + PR ingest with rateLimit logging; returns a summary dict.
- `django-celery-results` enabled (when `CELERY_RESULT_BACKEND=django-db`) shows task status/name/result in admin.
- Commit history harvest (planned):
  - SHA-anchored GraphQL query (`commit_history_from_sha`) to walk commit history from a given head backwards; avoids reliance on current PR ancestry after force-pushes.
  - Service helper to page history (`first/after/since`) with caps on pages/size.
  - Cursor table `CommitHistoryHarvest` (pull_request FK, start_sha, cursor, has_more, cutoff_ts, last_harvested_at) resumes paging across runs and low page budgets; periodic sweep task re-enqueues rows with `has_more=True`.
  - Harvest task enqueues `sync_ci_for_shas_task` for harvested heads missing CI.
  - Analyzer orchestrator calls the harvest task for force-push before/after SHAs; follow-up passes of `process_pr` should occur after CI lands to rebuild revisions/windows.

Notes
- Snapshots only: `statusCheckRollup` returns the latest state per context on each commit. Timeline and CI commit windows are capped by `$timelineK` and `$commitsM`.

## Historical Consistency and Force Pushes (V1)
- What force pushes do
  - A force‑push rewrites the PR branch history; older commits may disappear from the PR UI along with their visible statuses.
  - GraphQL rollups still expose latest statuses for commits that remain in the PR; disappeared commits won’t be listed.
- V1 invariants for consistency
  - We persist all snapshots we ingest and never delete them; old commits’ snapshots remain as history even if the PR branch is rewritten.
  - Queue eligibility is evaluated from the current head SHA only. On head change (new commits or force‑push), we close the prior interval and start a new one.
  - We ingest key timeline events (labels, draft toggles, reopened/closed) and will treat head changes as boundaries (via `HeadRefForcePushedEvent` or by detecting head SHA changes between syncs).
- What can go wrong during backfills
  - Missing older heads: if we never ingested CI for a past head SHA, total “time on queue” before our observation point may be undercounted or marked unknown.
  - Flapping checks on the same head: without CI history (StatusContext only has `createdAt`), we can miss fail→pass→fail transitions; we favor conservative undercounting over overcounting.
  - Hidden statuses after force‑push: UI hides old statuses; we don’t rely on UI, but if we never saw those statuses, we can’t reconstruct them without a targeted backfill.
- Mitigations in V1
  - Head‑centric intervals: treat any head change as an interval boundary; do not carry a green state across heads.
  - Fixed commit window: ingest contexts for the last `M` commits (small, configurable); optionally allow a few extra pages by cap. No time‑based cutoff for commits in V1.
  - Timeline “since”: use `timelineItems(since=last_synced_at−ε)` to reduce redundant fetch while retaining boundary events.
  - CI refresh task (future in V1): a lightweight periodic task to refresh the head commit’s contexts, since CI flips don’t always bump `updatedAt`.
  - Coverage flags: when we lack early timeline events or past head pass times, flag intervals as partial/unknown; downstream analytics can exclude or annotate.
  - On‑demand backfill: provide knobs to raise K and add a couple of commit pages for specific PRs where history matters.
- Out of scope for V1 (future extensions)
  - Full commit history storage (Commits/PRCommit tables) and CI change history via REST/checkSuites.
- Time‑based commit cutoffs (e.g., by commit timestamps) are unreliable across rebases/force‑pushes; V1 avoids them in favor of a fixed commit window with caps.

### Generating a Bundle (dev)
- Authenticate gh: `gh auth status` (or set `GH_TOKEN`).
- Run the bundle query for a single PR and write to JSON:
  ```bash
  gh api graphql \
    -F query=@qb_site/syncer/queries/pr_bundle.graphql \
    -F owner='leanprover-community' -F name='mathlib4' \
    -F number=30723 -F timelineK=150 -F commitsM=15 \
    -F timelineSince='2025-10-20T00:00:00Z' \
    > pr-30723.json
  ```
  If your shell/gh doesn’t expand `@file`, use:
  ```bash
  gh api graphql \
    -F query="$(< qb_site/syncer/queries/pr_bundle.graphql)" \
    -F owner='leanprover-community' -F name='mathlib4' \
    -F number=30723 -F timelineK=150 -F commitsM=15 \
    -F timelineSince='2025-10-20T00:00:00Z' \
    > pr-30723.json
  ```
- Sanity‑check shape with `jq`:
  - PR core: `jq '.data.repository.pullRequest.number' pr-30723.json`
  - Timeline types: `jq -r '.data.repository.pullRequest.timelineItems.nodes[].__typename' pr-30723.json | sort | uniq -c`
  - Commit oids: `jq -r '.data.repository.pullRequest.commits.nodes[].commit.oid' pr-30723.json`
  - Context types per commit: `jq -r '.data.repository.pullRequest.commits.nodes[].commit.statusCheckRollup.contexts.nodes[].__typename' pr-30723.json | sort | uniq -c`

## Current Functionality: Orchestration and Timeline Backfill
- Repo-level tasks
  - `syncer.sync_active_repos`: beat-dispatched, enqueues `syncer.sync_repo_since` for each active repo.
  - `syncer.sync_repo_since`: discovers updated PRs since a moving cutoff and enqueues `syncer.sync_pr` with conservative batching.
  - Rate guard/continuation: after discovery, if `remaining <= SYNCER_RATE_REMAINING_MIN`, the task defers and schedules a continuation at `resetAt + jitter`, debounced via Redis.
- PR-level task
  - `syncer.sync_pr`: header preflight skip when unchanged; otherwise ingests one bundle page and persists labels, key timeline events, and CI snapshots for the head commit window.
  - Backfill-only on skip: when unchanged, spends a small budget on older timeline pages (backward) and marks `timeline_backfill_cursor/done` and `timeline_earliest_synced_at`.
  - Rate guard: both the skip-path and the post-bundle pagination respect `SYNCER_RATE_REMAINING_MIN` and avoid backfill/pagination when budget is low.
- Settings (selected)
  - `SYNCER_RATE_REMAINING_MIN` (int): low budget threshold for deferral/guard.
  - `SYNCER_TIMELINE_K_DEFAULT`, `SYNCER_COMMITS_M_DEFAULT`: bundle sizes.
  - `SYNCER_REPO_ENQUEUE_BATCH_MAX`, `SYNCER_EST_COST_PER_PR`: repo batching knobs.
  - `SYNCER_DISCOVERY_LOOKBACK_MINUTES`, `SYNCER_DISCOVERY_LIMIT`, `SYNCER_DISCOVERY_STATES_DEFAULT`.

## Current Functionality: Commit Backfill (head-window history)
- Behavior
  - Adds a small, budgeted backfill for commits, mirroring timeline backfill.
  - Runs in two places:
    - Up-to-date path ("backfill_only"): page the PR’s commits connection backward by N pages and persist CI contexts for each commit on those pages.
    - Synced path: after normal bundle ingest, optionally spend N pages on older commits.
  - Per-PR state for visibility in admin:
    - `PullRequest.commits_backfill_cursor`
    - `PullRequest.commits_backfill_done`
    - `PullRequest.commits_earliest_synced_at` (min of page timestamps observed from CheckRun.completedAt/startedAt and StatusContext.createdAt).
  - Backfill is guarded by `SYNCER_RATE_REMAINING_MIN` in both paths.
- Settings
  - `SYNCER_COMMITS_BACKFILL_PAGES` (int, default 0): pages of older commits to fetch per run (up-to-date or synced). Set to 1–2 to enable.
- Admin
  - PR list shows `timeline_backfill_done` and `commits_backfill_done`.
  - PR detail shows the backfill cursors/done flags and earliest timestamps; also inlines for recent timeline events, check runs, and status contexts; object tools to "Enqueue sync" (respects backfill settings).

## Current Functionality: CI by SHA (for Analyzer/ad‑hoc backfill)
- API surface
  - Query: `syncer/queries/ci_by_commit.graphql` (contexts by commit SHA, includes `associatedPullRequests`).
  - Service: `syncer.services.ci_by_sha_service.sync_ci_for_sha(pr, sha, max_pages=..., require_pr_association=False)`.
  - Task: `syncer.sync_ci_for_shas(repo_id, number, shas=[...], max_pages_per_sha=?, require_pr_association=?)` with rate guard + continuation.
- Admin
  - PR page tool “Enqueue CI by SHA” accepts SHAs, optional pages per SHA and dry‑run; defaults to a strict PR‑association guard.
  - Additional Analyzer tools on PR page: “Rebuild revisions” and “Enqueue missing CI” leverage Analyzer services (read‑only actions; Syncer remains rate‑aware gate for CI fetches).
- Notes
  - The strict association guard is conservative and may exclude old heads after force‑pushes; Analyzer backfills should disable it and rely on PRRevision membership instead.

## Current Functionality: Metrics
- `SyncerMetricsSnapshot` stored every 15 minutes (Celery beat):
  - Counts of repo/PR tasks, deferrals/failures, coarse token usage (from `rate_events`), and DB size.
  - Admin list shows snapshots; a repository tools page includes a "Collect metrics now" button for ad-hoc sampling.

## Environment Knobs (summary)
- Backfill budgets: `SYNCER_TIMELINE_BACKFILL_PAGES`, `SYNCER_COMMITS_BACKFILL_PAGES`.
- Bundle defaults: `SYNCER_TIMELINE_K_DEFAULT`, `SYNCER_COMMITS_M_DEFAULT`.
- Rate/batching: `SYNCER_RATE_REMAINING_MIN`, `SYNCER_REPO_ENQUEUE_BATCH_MAX`, `SYNCER_EST_COST_PER_PR`.
- Discovery: `SYNCER_DISCOVERY_LOOKBACK_MINUTES`, `SYNCER_DISCOVERY_LIMIT`, `SYNCER_DISCOVERY_STATES_DEFAULT`.

## Planned Additions: CI Backfill Across Force-Pushes
- Derived “head revision windows” in Analyzer (tentative model `PRRevision` with `{pr, head_sha, from_ts, to_ts}`) built from force-push timeline events and header state.
- Analyzer computes which historical SHAs lack CI and enqueues Syncer requests to fetch CI for those SHAs over time (decoupled coordination).
- Query helpers in Analyzer reconstruct CI state “as of T” for a PR by picking the head SHA window covering `T` and joining the closest per-context CI records.
