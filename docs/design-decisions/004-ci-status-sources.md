# CI Status Sources: CheckRuns vs StatusContexts (Snapshots vs History)

## Context
- Goal: determine when a PR is “on the queue”. One condition: all essential CI jobs must have succeeded.
- We need CI signals that are reliable, low‑cost to ingest, and sufficient for v1 parity with the legacy dashboard.
- Two GitHub signals exist per commit:
  - CheckRun (GitHub Checks API; granular job attempts)
  - StatusContext (legacy Statuses API; append‑only statuses per named context)
- GraphQL’s `statusCheckRollup` exposes the latest state per “context” for both kinds, but not full history.

## Decision
- Use GraphQL `statusCheckRollup.contexts` as the primary source for current CI signals per commit.
  - Ingest CheckRun snapshots (latest per context) into `syncer.CheckRun`.
  - Ingest StatusContext snapshots (latest per context) into `syncer.StatusContext` as well.
- Do not fetch historical run attempts via `checkSuites → checkRuns` or REST in v1.
  - Keep REST Statuses API for optional backfill if a repository relies on StatusContext history.
- Compute coarse CI (`pass` / `fail` / `fail‑inessential` / `running` / `missing`) in Analyzer from snapshots, with an optional path to incorporate historical StatusContexts later.

## Consequences
- Pros
  - One GraphQL “PR bundle” fetch per changed PR; low request volume.
  - Sufficient for v1 queue filters that need the latest CI outcome (parity with legacy).
  - Simple ingestion: upsert CheckRun by GraphQL id; no heavy joins.
  - Clean boundary: Syncer stores raw facts; Analyzer derives coarse CI transitions.
- Cons
  - No per‑commit run‑attempt history in v1; cannot reconstruct earlier fail→pass windows on the same SHA.
  - If CI flaps on the same commit, total queue time cannot subtract those fail/running windows without historical data.
  - StatusContext history requires REST and extra requests when enabled.

## Background
- CheckRun (Checks API)
  - Represents a job attempt in a check suite. Has `status` (QUEUED/IN_PROGRESS/COMPLETED), `conclusion` (SUCCESS/FAILURE/etc.), timestamps (`startedAt`, `completedAt`).
  - In GraphQL rollup, CheckRun appears as a context node with the latest attempt per “context” on that commit.
- StatusContext (Statuses API)
  - Legacy per‑context status entries; append‑only. Has `state` (SUCCESS/FAILURE/ERROR/PENDING), `targetUrl`, `description`, `createdAt`.
  - GraphQL rollup returns only the latest per context. Full history requires REST: `GET /repos/{owner}/{repo}/commits/{sha}/statuses`.
- Our models (syncer)
  - `qb_site/syncer/models/check_run.py` stores snapshots per commit context: id, name, status, conclusion, `gh_started_at`, `gh_completed_at`, `head_sha`.
  - `qb_site/syncer/models/status_context.py` stores status rows. For v1 snapshots from GraphQL, we will key by `github_node_id` (GraphQL global id). If history is enabled, we will also ingest REST rows keyed by `rest_id` (numeric). The model will accommodate either id (each conditionally unique).
- Latest in rollup
  - GraphQL `statusCheckRollup.contexts.nodes` is a union of `CheckRun` and `StatusContext` for a commit. We map by `__typename`.

## Operational Notes
- Ingestion (per PR)
  - Use a single bundle query with `commits(last: M) { commit { status { contexts { id __typename ... }}}}`.
  - Upsert `CheckRun` snapshots by `github_node_id`; set `head_sha`, map timestamps.
  - Upsert `StatusContext` snapshots by `github_node_id`; set `head_sha`, `gh_created_at`.
  - If history is enabled, schedule REST backfill per commit SHA and upsert rows by `rest_id`.
- Analyzer
  - Derive coarse CI from the latest CheckRun/StatusContext snapshots. Apply the repo‑scoped “inessential jobs” set (case‑insensitive match on `name`/`context`).
  - If StatusContext history is enabled, merge historical rows ordered by `gh_created_at` to produce finer‑grained transitions.
- Indexes
  - CheckRun: `(pull_request, gh_completed_at)` for chronological scans.
  - StatusContext: `(pull_request, gh_created_at)` for chronological scans.

## Alternatives
- GraphQL `checkSuites → checkRuns` (history)
  - Pros: history without REST; single request per PR bundle.
  - Cons: heavier queries; paging and nested limits to tune; more complex mapping/dedup.
- REST Statuses API only
  - Pros: full history for legacy contexts.
  - Cons: many requests (per commit), mixed with GraphQL; higher token usage.
- Compute on read
  - Pros: simplest storage.
  - Cons: repeated recomputation; poor performance for multiple readers.

## Future Work
- If we need multiple status changes on the same commit (e.g., subtract fail/running intervals):
  - Enable a “history mode” feature flag per repo.
  - Option A: extend the bundle to include `checkSuites → checkRuns` with capped limits; write attempts to `CheckRun`.
  - Option B: add a REST fetcher for `/commits/{sha}/statuses` and persist to `StatusContext`.
  - Update Analyzer to merge run attempts/status rows chronologically and refine `PRCIStatusEvent` transitions.
  - No schema migration required for CheckRun/StatusContext; update services and add tests.
