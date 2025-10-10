# Syncer Ingestion Plan (v1)

This document outlines the v1 GitHub ingestion architecture that powers the raw data tables in `syncer`. It optimizes for:
- Parity with the current queueboard (labels + CI + key timeline events)
- Low request volume (one GraphQL “PR bundle” per changed PR)
- Idempotent, testable modules
- Clean separation from Analyzer (derived CI transitions, status evolution)

See also: `docs/django_backend_plan.md` for the broader migration plan and model summaries. CI signal choices and tradeoffs are captured in `docs/design-decisions/004-ci-status-sources.md`.

## Goals
- Persist raw facts for open PRs so we can reproduce dashboards and compute analytics:
  - PullRequest core fields
  - Current labels (LabelDef + PRLabel)
  - Key timeline events (label add/remove; draft toggles; reopened/closed)
  - CI history from CheckRun snapshots and StatusContext snapshots (latest per commit context via statusCheckRollup)
- Minimize API calls while staying robust and easy to reason about.

## Data Sources: Single “PR Bundle” Query
- One GraphQL request per changed PR fetches all needed data:
  - pullRequest core: number/author/state/isDraft/title/body/createdAt/updatedAt/baseRefName/headRefName/headRepo owner+name/additions/deletions/changedFiles
  - labels: `labels(first: 100) { nodes { name color } }`
  - timelineItems: `first: K` and filtered to `[LABELED_EVENT, UNLABELED_EVENT, READY_FOR_REVIEW_EVENT, CONVERT_TO_DRAFT_EVENT, REOPENED_EVENT, CLOSED_EVENT]`
  - commits: `last: M` (head commit window), with:
    - status.contexts { id, __typename, context/state OR name/status/conclusion, targetUrl/detailsUrl, createdAt/startedAt/completedAt }
      (latest per context per commit; union of `StatusContext` and `CheckRun`)
- Tunable limits (defaults; adjust per repo in settings):
  - `K` (timeline items): ~150–200
  - `M` (commits): ~10–20
- Optional batching: alias multiple PR bundles in one GraphQL call once stable.

## Models and Ownership
- Syncer (raw facts):
  - `PullRequest`, `LabelDef`, `PRLabel`, `PRTimelineEvent`, `CheckRun` (snapshot via statusCheckRollup), `StatusContext` (snapshot via statusCheckRollup)
- Analyzer (derived semantics):
  - `PRCIStatusEvent` (coarse CI transitions: pass/fail/fail-inessential/running/missing) computed from CheckRun + StatusContext and the repo-configurable “inessential jobs” set.

## Ingestion Flow (Per Repository)
1) Discover changed PRs
- GraphQL search for open PRs, ordered by `updatedAt`, gated by `last_synced_at` (or a moving window).

2) Fetch bundle per PR (changed only)
- Execute the PR bundle; cap using K/M.

3) Persist in a single transaction per PR
- PullRequest: upsert by `(repository, number)` from bundle core; set `last_synced_at`.
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

## Services & File Organization
- `syncer/services/github_client.py`
  - `GitHubGraphQLClient.execute(...)`
  - `GitHubClient.get_changed_pr_numbers(owner, name, since)`
  - `GitHubClient.get_pr_bundle(owner, name, number, limits)`
- `syncer/queries/pr_bundle.graphql` (single source of truth for the bundle)
- `syncer/services/pr_sync_service.py`
  - `PRSyncService.sync_repository(repo, since, limits)`
  - `PRSyncService.sync_pull_request(repo, number, limits)`
- Sub‑syncs (pure mapping + DB writes):
  - `services/sub/pull_request_sync.py`: `upsert_pull_request(...)`
  - `services/sub/labels_sync.py`: `sync_label_catalog(...)`, `sync_pr_labels(...)`
  - `services/sub/timeline_sync.py`: `sync_timeline_events(...)`
  - `services/sub/ci_sync.py`: `sync_check_runs(...)` (snapshots) and `sync_status_contexts(...)` (snapshots)
- Tasks & CLI:
  - `syncer/tasks/sync_tasks.py`: `sync_repo_task`, `sync_pr_task`
  - `syncer/management/commands/sync_repo.py`: repo runner with `--since` and bundle limits

## Incremental & Backfills
- Incremental gating: skip bundle when PR `updatedAt < last_synced_at`.
- Backfill: “deep CI” for a PR by temporarily increasing `M` for commits; optionally fetch REST statuses for those SHAs.

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
