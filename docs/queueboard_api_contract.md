# Queueboard API Contract (draft)

This document captures the contract for the Django/DRF replacement of the legacy `src/queueboard/api/*.json` artifacts and records how the current database schema covers the required fields. The goal is to expose a versioned API that the existing dashboard generator can consume with no behavioural changes, then deprecate filesystem JSON downloads.

## Scope and versioning
- Base path: `/api/v1/queueboard/`.
- Primary endpoint: `GET /api/v1/queueboard/snapshot?repo=<owner/name>` returning a JSON object that embeds the artifacts previously written to `api/*.json`.
- Versioning: bump the `v1` prefix for breaking changes; include `schema_version` and `generated_at` in the response metadata.

## Payload shapes
- Types reused from the legacy pipeline:
  - `Label`: `{ "name": str, "color": str, "url": str }`
  - `CIStatus` enum: `pass | fail | fail-inessential | running | missing`
  - `PRStatus` enum (from `classify_pr_state`): `queue | merge-conflict | awaiting-ci | awaiting-author | awaiting-zulip | blocked | delegated | ready-to-merge | awaiting-review | not-ready | closed`
  - `DataStatus`: `valid | incomplete | missing`
- `BasicPRInformation` (matches open-PR listings):
  ```json
  {
    "number": int,
    "author_name": str | null,
    "title": str,
    "url": str,
    "labels": [Label],
    "updatedAt": datetime
  }
  ```
- `AggregatePRInfo` (per PR, keyed by number in the snapshot):
  ```json
  {
    "is_draft": bool,
    "CI_status": CIStatus,
    "base_branch": str,
    "branch_name": str,
    "head_repo": str,
    "state": str,                // open|closed
    "last_updated": datetime,
    "author": str,
    "title": str,
    "description": str,
    "direct_dependencies": [int],
    "labels": [Label],
    "additions": int,
    "deletions": int,
    "modified_files": [str],     // first 100 paths
    "number_modified_files": int,
    "approvals": [str],
    "assignees": [str],
    "users_commented": [DataStatus, [str]],
    "number_total_comments": int | null,
    "last_status_change": {
      "status": DataStatus,
      "time": datetime,
      "delta": relativedelta,
      "current_status": PRStatus
    } | null,
    "first_on_queue": [DataStatus, datetime|null] | null,
    "total_queue_time": {
      "status": DataStatus,
      "value_td": timedelta,
      "value_rd": relativedelta,
      "explanation": str
    } | null
  }
  ```
- Snapshot response structure (single HTTP call replaces the legacy directory of files):
  ```json
  {
    "meta": {
      "schema_version": "v1",
      "generated_at": datetime,
      "repository": "owner/name",
      "rule_set_id": "<ruleset-id-or-name>"
    },
    "artifacts": {
      "aggregate_info": { "<pr_number>": AggregatePRInfo, ... },
      "draft_PRs": [BasicPRInformation],
      "nondraft_PRs": [BasicPRInformation],
      "CI_status": { "<pr_number>": CIStatus },
      "base_branch": { "<pr_number>": str },
      "all_pr_status": { "<pr_number>": PRStatus },
      "prs_to_list": { "<dashboard>": [BasicPRInformation] },
      "automatic_assignments": { "<pr_number>": "<github_login>" },
      "area_stats": { ... },              // same shape as legacy compute_area_ratios
      "dependency_graph": { "nodes": [...], "links": [...], "metadata": {...} }
    }
  }
  ```
- Optional additions:
  - `etag`/`last_modified` headers; `max_age` cache hints for consumers.
  - Future pagination hooks for per-dashboard lists if payload size grows.

## Database coverage (current vs. needed)
- Covered today:
  - Core PR metadata: state, draft, timestamps, base/head refs, head repo owner/name, title/body, additions/deletions, `changed_files_count` (Syncer `PullRequest`).
  - Engagement fields: first 100 `files`, `assignees`, `approvals` (approving review authors), `commenters` (issue + review authors), `number_total_comments`, with completeness flags and `engagement_synced_at`.
  - CI rollup inputs: `CheckRun`, `StatusContext`.
  - Labels and attachments: `LabelDef`, `PRLabel`.
  - Timeline events needed for state evolution: `PRTimelineEvent` (label add/remove, draft toggles, reopen/close).
  - Identity: `Repository`, `User`; reviewer preferences exist in `ReviewerPreference`.
  - Direct dependencies: parsed from `PullRequest.body` into Analyzer `PRDependency` via body-checkbox parsing; rebuild tasks (`analyzer.rebuild_pr_dependencies`, `analyzer.rebuild_dependencies_sweep`) keep edges in sync with existing PRs.
- Remaining considerations:
  - `head_repo` string still needs to be formatted from stored owner/name in the API response.
  - Map completeness flags → `DataStatus` consistently (e.g., `files_incomplete` → `incomplete` for `modified_files`, same for comments/reviews/assignees).
  - Queue inputs: replace `queue.json` with DB-derived queue computation; `use_aggregate_queue=True` should become the default once parity is proven.

## Implementation notes
- Coverage updates (Dec 2025):
  - Syncer now fetches and stores snapshot fields on `PullRequest`: first 100 `modified_files`, `assignees`, `approvals` (approving review authors), `commenters` (issue comment + review authors), and `number_total_comments` (issue + review comments) with completeness flags.
  - GraphQL bundle expanded to include files/assignees/reviews/comments/reviewThreads totals; comments/review bodies are not fetched.
  - Engagement backfill task (`syncer.backfill_repo_engagement[_active]`) enqueues PR syncs for rows that have never had the new fields populated (`engagement_synced_at IS NULL`). Schedule is configurable via `SYNCER_ENGAGEMENT_BACKFILL_*` env vars.
  - Body dependency edges: Analyzer `PRDependency` rows parse ``- [ ] depends on: #<n>`` lines from PR bodies; rebuild tasks (`analyzer.rebuild_pr_dependencies`, `analyzer.rebuild_dependencies_sweep`) keep dependencies for existing PRs up to date.
  - Dependency sweep runner: `analyzer.rebuild_dependencies_sweep` walks active repos in least-recently-checked order (tracked in `PRDependencyState`) so periodic runs eventually cover all PRs even after downtime; supports fan-out mode to limit memory on small dynos and uses a builder_version flag for safe re-parses.
- Source of truth for classification remains the existing Python logic (`classify_pr_state`, `ci_status`, `state_evolution`); port into Analyzer services and add parity tests using the fixtures in `test/`.
- Expose the snapshot via DRF; add Celery to precompute and cache the artifacts (`QueueSnapshot` table or cached blob).
- Add an `--source api` mode to `src/queueboard/dashboard_data.py` to fetch the snapshot and emit the same files for the renderer; run dual pipelines in CI to diff outputs before flipping the default.

## Open choices before API build
- Status mapping: translate completeness flags to `DataStatus` (`missing` if not synced, `incomplete` if caps/flags set, otherwise `valid`) for files/assignees/reviews/comments; mirror legacy behaviour when counts hit pagination caps.
- Head repo string: choose a single formatting helper (owner/name) for `head_repo` in `AggregatePRInfo`.
- Queue source: switch to DB-derived queue (no `queue.json`) and decide whether to keep a comparison/debug endpoint.
- Caching: pick defaults for ETag/Last-Modified and any Redis-backed caching of the snapshot.
- Dependency graph source: prefer `PRDependency` table over re-parsing bodies at response time.

## Snapshot builder plan
- Ownership: Analyzer service builds a `QueueboardSnapshot` payload; DRF endpoint serves cached/precomputed blobs keyed by `repo` + `rule_set_id`.
- Rule sets: resolve a single rule set per snapshot (repo default + optional override); include `rule_set_id` in meta and cache keys; no mixing across rule sets.
- DataStatus mapping:
  - `missing` if engagement/timeline data has never been synced (e.g., `engagement_synced_at` null or no timeline events).
  - `incomplete` if pagination caps hit or completeness flags are true (`files_incomplete`, `assignees_incomplete`, `reviews_incomplete`, `comments_incomplete`, timeline backfill not done).
  - `valid` otherwise.
- Inputs:
  - `PullRequest` with engagement fields/flags, `last_synced_at`, `engagement_synced_at`, timeline backfill flags.
  - `PRLabel`/`LabelDef`, `PRTimelineEvent`, `CheckRun`/`StatusContext`, `PRDependency`, reviewer preferences.
- Steps:
  1) Fetch open PRs for the repo; prefetch labels/engagement fields.
  2) Build `AggregatePRInfo` per PR from stored fields; map completeness→`DataStatus`; format `head_repo` from owner/name.
  3) Compute coarse `CI_status` from `CheckRun`/`StatusContext` (legacy `determine_ci_status` rules).
  4) Classify labels/CI/draft into `PRStatus` via `classify_pr_state` with the selected rule set.
  5) Timeline-derived stats (`last_status_change`, `first_on_queue`, `total_queue_time`) from `PRTimelineEvent`; apply DataStatus mapping based on sync/backfill state.
  6) Compute dashboards (`prs_to_list`) and stale metrics from the DB (replace `queue.json`).
  7) Reviewer suggestions/area stats: reuse legacy logic on the in-memory `AggregatePRInfo`.
  8) Dependency graph: use stored `PRDependency` edges to build nodes/links.
- Caching/precompute:
  - Option A: compute on request and return with ETag/Last-Modified.
  - Option B (preferred): Celery task to precompute and cache/persist snapshots; web serves cached blob; admin/CLI can force refresh; TTL-based cleanup for old/closed PR snapshots.

## After the snapshot builder
- DRF endpoint: expose `GET /api/v1/queueboard/snapshot` using the builder; add ETag/Last-Modified and optional cache headers; accept `rule_set_id` override.
- Worker wiring: schedule periodic precompute for active repos/rule sets; add admin/CLI “refresh snapshot” actions; purge stale snapshots on TTL and when PRs close.
- Legacy bridge: add `--source api` to `src/queueboard/dashboard_data.py`, keep filesystem path as a fallback; run dual-pipeline diff in CI until parity is proven.
- Parity tests: compare API snapshot output to legacy `api/*.json` from fixtures (including engagement fields, dependency graph, dashboards, DataStatus flags).
- Observability: log/summarize snapshot generation (counts, timings, rule_set_id, stale/missing data); surface metrics for cache hits/misses and rebuild durations.
