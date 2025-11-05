# Force‑Push Events Storage: Inline vs Separate Table

## Context
- We need reliable head‑change boundaries for Analyzer to segment “on‑queue” intervals per head SHA.
- GitHub GraphQL exposes `HeadRefForcePushedEvent` on a PR timeline with:
  - `id`, `createdAt`, `beforeCommit { oid }`, `afterCommit { oid }`.
- Our current timeline model is `syncer.PRTimelineEvent` capturing key events (labels, draft toggles, reopened/closed) with a conditional unique on `github_node_id` and an index on `(pull_request, occurred_at)`.
- Two viable storage approaches:
  - Inline into `PRTimelineEvent` as another event type with optional SHA columns.
  - Create a dedicated `PRHeadChange` table with required `before_sha`/`after_sha` fields.

## Decision
- For V1, inline force‑push events into `PRTimelineEvent`.
  - Add a new enum value: `HEAD_FORCE_PUSHED`.
  - Add nullable columns on `PRTimelineEvent`: `before_sha` (char(40)), `after_sha` (char(40)).
  - Keep idempotency by `github_node_id` (GraphQL id) using the existing conditional unique.
  - Add a check constraint ensuring `before_sha`/`after_sha` are set only when `type = HEAD_FORCE_PUSHED`.
  - Add an index on `(pull_request, after_sha)` to quickly locate a boundary by new head.
  - Analyzer treats `HEAD_FORCE_PUSHED` as a hard interval boundary.

## Consequences
- Pros
  - Single chronological stream for replay (no join to another table).
  - Minimal migration and ingestion changes; admin remains simple.
  - SHAs available as watermarks for targeted backfills and for correlating with CI snapshots.
- Cons
  - Type‑specific null columns on a shared table; requires a constraint to prevent misuse.
  - If future head‑change metadata grows (actor/ref), we’d add more nullable columns; may favor a separate table later.
- Performance/Cardinality
  - Force‑pushes are rare; added columns and indexes have negligible impact at V1 scale.

## Operational Notes
- Queries
  - Extend `qb_site/syncer/queries/pr_bundle.graphql` and `timeline_page.graphql` to include `HEAD_REF_FORCE_PUSHED_EVENT` nodes with:
    - `id`, `createdAt`, `beforeCommit { oid }`, `afterCommit { oid }`.
- Ingestion
  - Extend `syncer/services/sub/timeline_sync.py` `type_map` to handle `HeadRefForcePushedEvent → HEAD_FORCE_PUSHED` and persist SHAs (when present).
  - Preserve idempotency via `github_node_id`.
- Migrations
  - Alter `syncer.PRTimelineEvent` to add `before_sha`, `after_sha` (char(40), null=True),
    a check constraint binding SHAs to `HEAD_FORCE_PUSHED`, and an index on `(pull_request, after_sha)`.
- Analyzer
  - Close any open “on‑queue” interval at `HEAD_FORCE_PUSHED.createdAt` and start a new segment for `after_sha`.
  - Continue to evaluate current queue state from the latest head SHA only.

## Alternatives
- Separate `PRHeadChange` table
  - Pros: cleaner schema (required SHAs, purpose‑built indexes, easier to evolve: ref, actor).
  - Cons: extra model, migration, ingestion path, and a second stream to merge for full history.
- Omit SHAs (timestamp only)
  - Simpler schema but loses a useful watermark for backfills and weakens correlation to CI snapshots.
- Infer head changes without events
  - Detect head SHA drift between syncs; useful as a fallback but less explicit than timeline events and can miss intermediate rewrites.
