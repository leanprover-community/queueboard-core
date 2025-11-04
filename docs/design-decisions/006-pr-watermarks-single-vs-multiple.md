# PR Sync Watermarks: Single vs Multiple

## Context
- We sync GitHub PR data in two main surfaces:
  - Timeline items (close/reopen, label adds/removes, etc.) via `pullRequest.timelineItems` which supports a `since` filter.
  - CI snapshots (CheckRun and StatusContext via `statusCheckRollup`) retrieved per-commit; no native `since` filter on contexts.
- We need to be token-efficient, resume cleanly after rate-limit pauses, and keep the system simple and reliable.
- Two plausible tracking strategies:
  - Single watermark per PR: `PullRequest.last_synced_at` advanced only after a complete ingest.
  - Multiple watermarks per PR: e.g., `timeline_synced_through` and `commits_synced_through` advanced independently.
- Related docs:
  - docs/design-decisions/004-ci-status-sources.md (CI sources and snapshot strategy)
  - docs/design-decisions/005-page-until-cutoff-pagination.md (paging approach)

## Decision
- For V1, use a single per-PR watermark: `PullRequest.last_synced_at`.
- Do not persist section-specific watermarks for timeline vs. commits.
- Apply the watermark like so:
  - Timeline: query with `since = last_synced_at - epsilon` (small backoff to avoid boundary misses) and page forward (`after`) while `hasNextPage`.
  - CI snapshots: fetch newest commits (large page size; optionally page older) with a small cap; no time-based cutoff for commits in V1.
- Don’t persist cursors in DB. If we stop mid-PR due to rate limit, we resume next run using the same `since`; idempotent upserts by GitHub node IDs ensure correctness.
- Only update `last_synced_at` to the PR header `updatedAt` after a successful full ingest of that PR; partial runs do not advance it.

## Consequences
- Simplicity: fewer moving parts, no extra migrations or drift risks between multiple watermarks.
- Correctness: re-fetching recent pages is safe due to idempotent keys:
  - Timeline events by `github_node_id`.
  - Labels: `LabelDef` case-insensitive per repo; `PRLabel` unique by (PR, label).
  - CI snapshots by `github_node_id` for CheckRun and StatusContext.
- Predictable resume: stateless retry after `resetAt`; no special state to carry over.
- Token trade-off: may re-request some recent pages per PR, especially on partial runs; acceptable for V1 with preemptive budget guards.
- CI-only changes may not move `PR.updatedAt`; we’ll add a lightweight “head-commit CI refresh” scheduled task as needed rather than tracking a separate commit watermark now.

## Operational Notes
- Queries
  - `qb_site/syncer/queries/pr_bundle.graphql`: add/pass `$timelineSince` and use on `timelineItems(since: $timelineSince)`.
  - `qb_site/syncer/queries/timeline_page.graphql`: same `since` + `after` paging.
  - `qb_site/syncer/queries/commits_page.graphql`: page newest→older with a small cap; no time-based cutoff.
- Services
  - `qb_site/syncer/services/pr_sync_service.py`: implement “page-until-cutoff” loops; enforce a preemptive rate-limit threshold before each call.
  - `qb_site/syncer/services/github_client.py`: capture `rateLimit { remaining resetAt cost }` after every call; raise a typed error on 403.
- Tasks
  - `qb_site/syncer/tasks/sync_tasks.py`: sequential per-repo orchestration with a per-repo Redis lock; stop early on low budget and resume next tick.
- Epsilon handling: subtract 1–2 seconds from `last_synced_at` when passing `since` to avoid dropping boundary-equal events.
- Instrumentation: log per-query costs and remaining tokens; sample duplicate-item rate to decide if multiple watermarks are worth adding later.
- No migration required beyond the existing `PullRequest.last_synced_at` field.

## Alternatives
- Multiple watermarks (deferred):
  - Add `timeline_synced_through` (max timeline item createdAt) and `commits_synced_through` (a commit-history semantic cutoff) per PR.
  - Advance each only after its section completes; use them as section-specific cutoffs.
  - Pros: fewer repeated pages on long backfills or very tight budgets.
  - Cons: more state, migrations, updating rules, tests, and risk of drift.
- Store/persist cursors:
  - Persist GraphQL cursors to resume mid-PR precisely.
  - Pros: minimal duplicate queries; faster resume.
  - Cons: brittle against force-pushes/history changes; extra state to validate; not necessary for V1.
- Commit-history(since) for CI:
  - Use `headRepository.ref(...).target ... on Commit { history(since:) }` to bound commits by time.
  - Pros: tighter CI window; fewer pages.
  - Cons: complexity with forks/permissions; defer until needed.
