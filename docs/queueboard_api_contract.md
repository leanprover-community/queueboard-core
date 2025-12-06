# Queueboard API Contract (draft)

This document captures the contract for the Django/DRF replacement of the legacy `src/queueboard/api/*.json` artifacts and records how the current database schema covers the required fields. The goal is to expose a versioned API that the existing dashboard generator can consume with no behavioural changes, then deprecate filesystem JSON downloads.

## Scope and versioning
- Base path: `/api/v1/queueboard/`.
- Primary endpoint: `GET /api/v1/queueboard/snapshot?repo=<owner/name>` returning a JSON object that embeds the artifacts previously written to `api/*.json`.
- Versioning: bump the `v1` prefix for breaking changes; include `schema_version` and `generated_at` in the response metadata.

## Minimal snapshot (current)

We now emit a single `snapshot.json` used by the dashboard renderer (dependency graph, area stats, and automatic assignments remain separate files/endpoints). Shape:

```json
{
  "meta": {
    "schema_version": "v1-draft",
    "generated_at": "<datetime>",
    "repository": "owner/name",
    "rule_set_id": "<ruleset-id-or-name>"
  },
  "prs": {
    "<pr_number>": {
      "state": "open|closed",
      "is_draft": true|false,
      "base_branch": "<branch>",
      "branch_name": "<head ref>",
      "last_updated": "<datetime>",
      "author": "<login>",
      "title": "<str>",
      "description": "<str>",
      "labels": [ { "name": "<str>", "color": "<hex>", "url": "<label url>" } ],
      "additions": 0,
      "deletions": 0,
      "modified_files": ["<path>", ...],        // first 100
      "number_modified_files": 0,
      "approvals": ["<login>", ...],
      "assignees": ["<login>", ...],
      "users_commented": ["valid|incomplete|missing", ["<login>", ...]] | null,
      "number_total_comments": 0 | null,
      "direct_dependencies": [int],
      "ci_status": "pass|fail|fail-inessential|running|missing",
      "pr_status": "<PRStatus or null>",
      "last_status_change": {
        "status": "valid|incomplete|missing",
        "time": "<datetime>",
        "delta": { "days": 1, "hours": 2, ... },
        "current_status": "<PRStatus>"
      } | null,
      "first_on_queue": ["valid|incomplete|missing", "<datetime|null>"] | null,
      "total_queue_time": {
        "status": "valid|incomplete|missing",
        "value_td": <seconds>,
        "value_rd": { "days": 1, "hours": 2, ... },
        "explanation": "<str>"
      } | null
    }
  },
  "lists": {
    "draft_prs": [int],
    "nondraft_prs": [int],
    "dashboards": { "<dashboard>": [int] }
  }
}
```

Notes:
- `dashboards` mirrors `prs_to_list.json`; the renderer rebuilds `BasicPRInformation` from `prs`.
- `dashboard.py` now prefers `api/snapshot.json` when present and falls back to the legacy `api/*.json` emitted by `dashboard_data.py`; those legacy files still use the custom type-wrapped JSON format.
- `prs_to_list` is currently computed with `use_aggregate_queue=False`, so `queue.json` from GitHub search remains the source of truth for queue membership until we replace it.
- Dependency graph, area stats, and automatic assignments stay separate for now.

Update cadence: ingest/upserts populate the raw fields; the snapshot builder computes `ci_status`, `pr_status`, dashboards, and uses precomputed timeline analytics when available (marking incomplete/missing via `DataStatus`).

## Current filesystem artifacts (post-refactor)
- Snapshot is the normalized contract; everything else is compatibility scaffolding for the legacy renderer.
- `aggregate_info.json`, `draft_PRs.json`, `nondraft_PRs.json`, `CI_status.json`, `all_pr_status.json`, and `prs_to_list.json` are still written for CLI consumers using `CustomJSONEncoder` wrappers (`__type__`, `__module__`, `__data__`). The API should avoid emitting this encoding; any adapter can regenerate these shapes from the snapshot.
- `automatic_assignments.json`, `area_stats.json`, and `dependency_graph.json` are copied verbatim into `gh-pages/` and will need server-side equivalents (or to be derived from the snapshot payloads).
- `queue.json` from GitHub search is still read by `determine_pr_dashboards` in `src/queueboard` when `use_aggregate_queue=False`.

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
  - Map completeness flags → `DataStatus` consistently (e.g., `files_incomplete` → `incomplete` for `modified_files`, same for comments/reviews/assignees).
  - Queue inputs: replace `queue.json` with DB-derived queue computation; `use_aggregate_queue=True` should become the default once parity is proven.

## Implementation notes
- Contract: treat the snapshot schema as canonical for the API; legacy type-wrapped files remain a client-side compatibility layer generated from the snapshot.
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
- Head repo string: choose a single formatting helper (owner/name) for `head_repo` in `AggregatePRInfo` if we decide to expose it; the snapshot currently omits it and defaults in the loader.
- Queue source: switch to DB-derived queue (no `queue.json`) and decide whether to keep a comparison/debug endpoint.
- Caching: pick defaults for ETag/Last-Modified and any Redis-backed caching of the snapshot.
- Dependency graph source: prefer `PRDependency` table over re-parsing bodies at response time.
- Legacy compatibility: prefer to ship only the normalized snapshot + supporting payloads from the API; regenerate legacy type-wrapped `api/*.json` on the client side when needed.

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

## Migration plan (post-refactor)
- Surface freeze: treat `snapshot.json` plus `automatic_assignments.json`, `area_stats.json`, and `dependency_graph.json` as the v1 contract; legacy type-wrapped files remain adapter-only.
- Server endpoints: build DRF views for `GET /api/v1/queueboard/snapshot` (rule_set override, ETag/Last-Modified) and sibling endpoints for assignments/area stats/dependency graph; allow bundling all in one payload for the CLI adapter.
- Server computation: port CI/status classification, dashboard partitioning (drop `queue.json`), dependency graph assembly, and reviewer suggestion/area stats into Analyzer services with pytest parity tests against `test/newtest`/`test_snapshot`.
- Precompute/caching: schedule Celery jobs to refresh snapshots and supporting payloads per repo/ruleset; add admin/CLI “refresh snapshot” actions; purge stale blobs on TTL and when PRs close.
- Client bridge: add `--source api` to `src/queueboard/dashboard_data.py` to fetch the snapshot/supporting payloads and re-emit legacy `api/*.json` locally for HTML generation; keep filesystem download as a fallback until parity is proven.
- Rollout: run dual pipelines and diffs in CI, cut over dashboard generation to the API source by default, then retire the filesystem download path and `queue.json`.
