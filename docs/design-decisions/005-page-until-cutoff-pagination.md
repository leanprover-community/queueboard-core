# Page‑Until‑Cutoff Pagination for PR Bundles

## Context
- We ingest a PR’s raw facts via GraphQL: key timeline events and CI snapshots from recent head commits.
- A single “snapshot window” (`timelineItems(first=K)`, `commits(last=M)`) may miss older items on long‑lived PRs.
- We need a strategy that is correct since a cutoff (e.g., `PullRequest.last_synced_at` or a backfill `since`), idempotent, and cost‑bounded.
- GraphQL requires pagination for both connections, and contexts live inside a union under `statusCheckRollup`.

## Decision
- Implement page‑until‑cutoff pagination for timeline events and a capped strategy for commits.
  - Timeline: use `timelineItems(since: cutoff)` and page forward with `first/after` until no next page (recent‑first window). This reduces the number of pages vs. scanning down to the cutoff.
  - Commits (V1): use a fixed window (`commits(last: M)`) and, if needed, a small number of extra pages capped by `SYNCER_COMMITS_MAX_PAGES`. We do not rely on commit timestamps for a cutoff due to rebases/force‑pushes and deprecated fields.
- Stream pages directly into sub‑sync services; rely on idempotency (unique GraphQL IDs) to dedupe.
- Bound work with small caps (per repo defaults): `SYNCER_TIMELINE_MAX_PAGES`, `SYNCER_COMMITS_MAX_PAGES`; log truncation.
- Use the single‑page snapshot window as the fast path when a cutoff is recent and likely covered; escalate to paging when first page’s minimum timestamp is still ≥ cutoff.

## Consequences
- Pros
  - Predictable correctness since cutoff (modulo configured caps).
  - Costs are bounded and observable (page counts, truncation logs).
  - Simple loops with one stopping predicate each; easy to test.
- Cons
  - More requests on large/old PRs compared to snapshot‑only.
  - Not atomic: the PR can update while paging; we embrace eventual consistency and idempotency.
  - Nested pagination for `contexts` is deferred; extremely large context sets (>100) may be truncated in v1.

## Operational Notes
- Configuration
  - Per‑repo defaults for `timelineK`, `commitsM`, plus `SYNCER_TIMELINE_MAX_PAGES`, `SYNCER_COMMITS_MAX_PAGES` (environment or settings).
  - Optional cutoff source: `PullRequest.last_synced_at` for incremental; a `--since` CLI flag for backfills.
- Telemetry
  - Log page counts, oldest/newest timestamps seen, and a `truncated=true` flag when caps are hit.
  - Optionally log GraphQL `rateLimit { remaining resetAt cost }` to tune throughput.
- Testing
  - Unit: client page helpers (pageInfo, cursors), cutoffs, and caps.
  - Integration: orchestrator loops over synthetic paged responses; assert idempotent writes and truncation behaviors.

## Alternatives
- Snapshot‑only
  - Simpler and cheaper but silently incomplete for long‑lived PRs.
- Deep/full history
  - Pages entire history (timeline, commits, and nested contexts); high cost with limited v1 value.
- Adaptive “as needed”
  - Start snapshot; escalate based on heuristics (e.g., missing READY_FOR_REVIEW). Lower average cost but higher control‑flow complexity and risk of silent misses if heuristics are incomplete.
