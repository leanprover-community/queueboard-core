# Sync Schema Versioning and Comment/Review Timeline Events

## Context
- The syncer captured only a narrow set of timeline events on
  `PRTimelineEvent` (labels, assigns, ready/draft/reopen/close, head
  force-pushes). Comments and review activity were stored only as
  aggregates on `PullRequest` (`commenters`, `approvals`,
  `number_total_comments`); there was no per-event log of who
  commented, who reviewed, or when.
- A previous expansion of captured fields (assignees, approvals,
  commenters, files, head_ci/head_sha) had introduced
  `engagement_synced_at` on `PullRequest` as a nullable timestamp plus
  a dedicated backfill task that selected PRs where the column was
  null. Repeating this pattern — one `*_synced_at` column per
  ingestion expansion — does not scale: it adds schema churn and
  bespoke backfill plumbing for every new feature.
- We needed a single, durable bookkeeping mechanism that lets future
  ingestion expansions land without further `PullRequest` schema
  changes, plus event-level coverage of issue comments, review
  submissions/dismissals/requests, and inline review comments
  (timestamps + actors + minimal type-specific fields, not bodies).

## Decision

### `sync_schema_version` upgrader framework
- `PullRequest.sync_schema_version: PositiveSmallIntegerField(default=0, db_index=True)`
  records the highest ingestion expansion satisfied for a PR.
- `qb_site/syncer/services/sync_schema_upgrades.CURRENT_SYNC_SCHEMA_VERSION`
  is the codebase-side target. Bumping the constant + registering an
  upgrader is how new "we want to capture X" expansions land — no new
  `*_synced_at` column.
- The upgrader registry is the *sole writer* of `sync_schema_version`.
  `PRSyncService` does not touch it. (Otherwise a routine sync that
  completes before the upgrader's rewalk would prematurely advance the
  version.)
- A periodic task (`syncer.upgrade_schema_versions[_active]`)
  dispatches per PR by walking `current_version + 1 .. CURRENT`:
  - **No upgrader registered** → auto-stamp and continue.
    Deliberate: trivial / already-satisfied versions need no
    stamper-only class (e.g. v=1, whose data is written on every
    `PRSyncService` sync). The trade-off is that a future bump that
    forgets to register an upgrader silently auto-stamps; mitigated
    by reviewer attention on the diff and a DEBUG log line per
    auto-stamp.
  - **Upgrader registered and `is_complete(pr)` True** → stamp.
  - **Otherwise** → call `kick(pr)` and stop iterating for this PR.
  - Stamping is a guarded `update(...)` keyed on `pk` and
    `sync_schema_version__lt=s` so concurrent dispatchers can't walk
    the column backward.
- Per-task pacing splits two workloads:
  `SYNCER_SCHEMA_UPGRADE_BATCH_SIZE` (default 1000, DB-only stamping)
  and `SYNCER_SCHEMA_UPGRADE_KICK_LIMIT` (default 20, GitHub-bound).
  An optional `SYNCER_SCHEMA_UPGRADE_TARGET_VERSION` env var clamps
  the wave below `CURRENT_SYNC_SCHEMA_VERSION` for staged rollouts /
  emergency halts.
- Convergence canary (in `SyncerConvergenceSnapshot`):
  `prs_below_current_sync_schema_version` per repo +
  `sync_schema_version_target`. A flat-or-growing line on the canary
  across snapshots is the signal of a stalled wave.

### Event types captured at v=3 (current)
The seven `PRTimelineEventType` values added by this design, all
sourced from GraphQL `timelineItems` and idempotent on
`github_node_id`:

| `type`                     | Source GraphQL type                   | Other typed columns                                         | `extra`                                                                                  |
| -------------------------- | ------------------------------------- | ----------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| `ISSUE_COMMENTED`          | `IssueComment`                        | —                                                           | `{}`                                                                                      |
| `REVIEW_APPROVED`          | `PullRequestReview` (state=APPROVED)  | `inline_comment_total_count`                                | `{}`                                                                                      |
| `REVIEW_CHANGES_REQUESTED` | `PullRequestReview`                   | `inline_comment_total_count`                                | `{}`                                                                                      |
| `REVIEW_COMMENTED`         | `PullRequestReview` (state=COMMENTED) | `inline_comment_total_count`                                | `{}`                                                                                      |
| `REVIEW_DISMISSED`         | `ReviewDismissedEvent`                | —                                                           | `{dismissed_review_node_id, dismissed_review_author, dismissed_review_submitted_at, previous_review_state}` |
| `REVIEW_REQUESTED`         | `ReviewRequestedEvent`                | one of `requested_reviewer_login` / `requested_team_slug`   | `{}`                                                                                      |
| `REVIEW_REQUEST_REMOVED`   | `ReviewRequestRemovedEvent`           | one of `requested_reviewer_login` / `requested_team_slug`   | `{}`                                                                                      |

New typed columns on `PRTimelineEvent`:
- `extra: JSONField(default=dict, blank=True)` — display-time
  denormalization, not filtered on.
- `requested_reviewer_login` / `requested_team_slug` — indexed,
  mutually exclusive (User/Bot/Mannequin → reviewer; Team → team).
- `inline_comment_total_count` — GitHub-truth `comments.totalCount`
  on `PullRequestReview`. Refreshes on rewalk.

CHECK constraints (`syncer_prtl_requested_reviewer_by_type_ck`,
`syncer_prtl_requested_reviewer_mutex_ck`,
`syncer_prtl_inline_total_by_type_ck`) enforce the by-type and mutual
exclusion rules at the DB layer.

### `PRReviewInlineComment` + `PRReviewInlineCommentBackfill`
- `PRReviewInlineComment` (one row per inline comment) mirrors
  GraphQL's `PullRequestReviewComment`. Linked to the parent review
  by `review_node_id` (durable) and a nullable `parent_review_event`
  FK (ORM convenience). Idempotent on globally-unique
  `github_node_id`. `thread_root_node_id` is a best-effort root of
  the `replyTo` chain, computed at ingest by walking the union of
  the in-flight set + existing rows in DB; monotone-toward-truth on
  rewalk (definitive walk UPSERTs, fallback walk INSERT-IGNOREs).
- `PRReviewInlineCommentBackfill` is a sync-state marker table for
  reviews whose nested `comments(first: K)` fetch hit the page limit
  (`hasNextPage = true`). Keyed on `review_node_id` (unique) with a
  nullable `review_event` FK so the marker survives even when
  synthesis can't fire (dismiss event with `review: null`). The
  table *is* the index of "needs work"; a v=4+ paginator can find
  outliers in O(rows-needing-work).
- `K = SYNCER_INLINE_COMMENTS_PER_REVIEW` (default 20).

### Removed: `engagement_synced_at`
- The column on `PullRequest` and the `backfill_repo_engagement[_active]`
  task it gated have been retired. After v=1, every PR has been
  re-synced under code that wrote both `engagement_synced_at` and
  `last_synced_at` on the same path, making `engagement_synced_at IS NULL`
  equivalent to `last_synced_at IS NULL` for read-side use. Read
  callers (`analyzer.queueboard_snapshot._data_status`, the
  `sync_pr_task` skip-decision) now consult `last_synced_at`.
- `prs_missing_engagement` and `prs_engagement_incomplete` were
  removed from `SyncerConvergenceSnapshot`; the
  `SYNCER_ENGAGEMENT_BACKFILL_*` settings + queue routes + beat
  schedule entries were deleted with the task.

## Consequences
- New ingestion expansions land as `(version bump, register
  upgrader, optional data-migration reset of `timeline_backfill_done`
  if a rewalk is required)`. No new `*_synced_at` column, no new
  bespoke backfill task.
- The aggregate fields `commenters` / `approvals` /
  `number_total_comments` on `PullRequest` are **soft-deprecated**:
  they are still computed from the bundle's `reviews(first: 100)` and
  `comments(first: 100)` connections at ingest, but the per-event
  log is now the preferred source for analytics. At the >100
  boundary the aggregates and the event log can disagree by design.
- Bot filtering happens at query time, not at ingest. All actors
  (User / Bot / Mannequin / null) are stored.
- The hot-path bundle query stays roughly flat: nested
  `comments(first: K)` extends the existing `PullRequestReview`
  fragment rather than adding a top-level connection. Bundle cost
  measured on busy real-world PRs is on the order of tens of KB —
  well within budget.
- Inline comments on dismissed reviews ingest with
  `parent_review_event=NULL` only when synthesis cannot fire (rare:
  GitHub returned `review: null`). They remain queryable via
  `review_node_id`.
- `PRReviewInlineCommentBackfill` rows accumulate without a consumer;
  see Deferred Follow-ups.
- Scan posture for the new periodic task is healthy:
  `WHERE sync_schema_version < CURRENT` is an indexed predicate
  returning O(rows-needing-work), not O(table-size). Verified
  via `EXPLAIN ANALYZE` post-deploy.

## Invariants
- **The upgrader registry is the sole writer of
  `sync_schema_version`.** `PRSyncService` does not touch it.
- **`PRTimelineEvent` rows correspond 1:1 to `timelineItems` nodes.**
  Inline review comments live in `PRReviewInlineComment`. Synthesized
  rows for dismissed reviews use the dismissed review's real node id
  (so a later walk surfacing the actual node refreshes its fields).
- **Persist events first, advance the timeline cursor second.** A
  crash between the two leaves a safe re-fetch state on retry. Same
  for `PRReviewInlineComment` relative to the cursor that brought us
  the parent reviews.
- **`REVIEW_DISMISSED` actor ≠ review author.** `actor_login` comes
  from `ReviewDismissedEvent.actor`. The dismissed review's
  identity, author, and submission timestamp are denormalized into
  `extra.dismissed_review_*`.
- **`REVIEW_DISMISSED` ingest synthesizes the dismissed review's
  parent row.** `state=DISMISSED PullRequestReview` nodes are dropped
  at row creation (their current state hides the original submission
  state); the dismiss event's `extra.previous_review_state` carries
  enough information to synthesize the corresponding `REVIEW_<state>`
  row, idempotent on `github_node_id`. This makes the data shape
  independent of sync timing. Synthesis runs on every ingest of a
  dismiss event, so it self-heals.
- **Pending reviews are dropped at ingest** (`submittedAt is null`).
- **Mannequins, bots, and deleted accounts.** Persist `actor_login`
  / `author_login` as `""` when the GraphQL value is null;
  use the mannequin/bot login when present. `Bot` is in the
  `RequestedReviewer` union and routes to `requested_reviewer_login`.
- **Idempotency on insert** via globally-unique `github_node_id`.
  Inserts use `bulk_create(..., ignore_conflicts=True)` except
  inline-comment thread-root reconciliation, which uses a split
  `update_conflicts=True` / `ignore_conflicts=True` two-batch
  upsert so wider-context rewalks improve stored values and
  narrower-context rewalks never regress them.
- **Three-call-site rule for sub-syncs nested under timeline items.**
  Bundle, forward-page, and back-page paths must all invoke the
  sub-sync. The schema-upgrade waves (`UpgradeToVN.kick`) drive the
  back path; missing wire-up there is a silent data-drop bug. See
  the syncer `AGENTS.md` "Timeline ingest invariants" subsection.
- **Force-rewalk uses `force=True` on `sync_pr_task`.** The dedupe
  TTL would otherwise swallow upgrader-triggered syncs.
- **Schema upgrades that need a rewalk reset `timeline_backfill_done`**
  in their data migration. Without the reset, PRs with a
  pre-bump-True flag short-circuit the new upgrader's `is_complete`
  to True and auto-stamp without rewalking. (Both v=2 and v=3 used
  this option-(a) approach. If a third such bug ever requires a
  third wave, that's the moment to invest in a
  `timeline_query_version` column on `PullRequest`.)

## Operational Notes
- Current `CURRENT_SYNC_SCHEMA_VERSION` = `3`.
- Active upgraders: `UpgradeToV2` (timeline rewalk under broader
  `itemTypes` + nested inline comments) and `UpgradeToV3`
  (mechanically identical, recovers PRs that completed the v=2 wave
  before the page-path inline-comment wire-up was fixed). Both
  registered via `SyncerConfig.ready()` in
  `qb_site/syncer/apps.py`.
- Periodic task: `syncer.upgrade_schema_versions[_active]` (see beat
  schedule). Pacing settings: `SYNCER_SCHEMA_UPGRADE_BATCH_SIZE` /
  `SYNCER_SCHEMA_UPGRADE_KICK_LIMIT` /
  `SYNCER_SCHEMA_UPGRADE_TARGET_VERSION`.
- Migrations of record (in this design's scope):
  `0039` (`sync_schema_version` + new typed columns on
  `PRTimelineEvent` + data-migrate stamp engagement-synced PRs to
  v=1), `0041` (inline-comment models), `0042` (new
  `PRTimelineEventType` enum values), `0043` (CHECK constraints),
  `0044` / `0045` (`timeline_backfill_done` resets for v=2 / v=3
  waves), `0046` (dismissed-review synthesis backfill + inline-comment
  schema tightening), `0047` (drop `engagement_synced_at` and the two
  removed `SyncerConvergenceSnapshot` engagement metrics).
- Sanitized backup policy: `PRReviewInlineComment` and
  `PRReviewInlineCommentBackfill` are retained (see
  `scripts/backup_policy.py`).
- Convergence canaries to watch:
  - `prs_below_current_sync_schema_version` per repo — must trend
    monotonically down pass-over-pass during a wave.
  - `PRReviewInlineComment` row count per repo — should grow in
    lockstep with the v=2 → v=3 transition cohort.
  - `PRReviewInlineCommentBackfill` row count — long-tail reviews
    (>K inline comments) produce rows here.
  - GitHub rate-limit telemetry — same posture as ongoing backfills.
- Post-deploy SQL spot-check (the regression signature from the v=2
  inline-comment gap, kept here for any future similar wave):
  ```sql
  -- Should return ~0; non-trivial counts mean a sub-sync isn't
  -- being invoked from one of the three timeline-ingest call sites.
  SELECT COUNT(*)
    FROM syncer_pullrequest pr
    WHERE pr.sync_schema_version >= 3
      AND EXISTS (SELECT 1 FROM syncer_prtimelineevent ev
                    WHERE ev.pull_request_id = pr.id
                      AND ev.type IN ('REVIEW_APPROVED', 'REVIEW_CHANGES_REQUESTED', 'REVIEW_COMMENTED')
                      AND COALESCE(ev.inline_comment_total_count, 0) > 0)
      AND NOT EXISTS (SELECT 1 FROM syncer_prreviewinlinecomment ic
                        WHERE ic.pull_request_id = pr.id);
  ```

## Deferred Follow-ups
- **`PRReviewInlineCommentBackfill` consumer.** The marker table is
  written reliably but has no consumer; rows accumulate without
  bound. A v=4+ paginator would walk
  `PullRequestReview.comments` past the K=20 cutoff and clear or
  mark rows complete.
- **Switch the soft-deprecated aggregates to the event log.**
  `commenters` / `approvals` / `number_total_comments` are still
  computed from the bundle's first-100 connections; switching their
  computation to `PRTimelineEvent` aggregates is a v=4+ task.
- **Capture `MERGED` events** (close cousin of `CLOSED`).
- **Promote `extra.previous_review_state` to a typed column** if
  reviewer-engagement queries grow to filter on it.
- **Capture the dismissed `PullRequestReview` row's
  `inline_comment_total_count` from the actual node**, not just
  from the dismiss event — minor; the synthesized parent's count is
  null until a later walk surfaces the real node.
- **Periodic re-resolution of thread roots from the durable
  `reply_to_node_id` graph** (defends against a hypothetical
  scenario where intermediate rows in a chain are deleted from the
  DB after ingest, leaving the leaf's stored root "stale-better"
  rather than "current-truth").
- **Migration locking posture.** Bulk `update(...)` data migrations
  ran cleanly at our current PR-table scale. If the table grows
  another order of magnitude before the next wave, chunk by
  `repository_id` or `id` ranges with a `RunPython` loop.
- **Other `timelineItems` types deliberately not captured at v=3:**
  `RENAMED_TITLE`, `BASE_REF_CHANGED`, `BASE_REF_FORCE_PUSHED`;
  cross-issue/PR linkage (`CROSS_REFERENCED`, `REFERENCED`,
  `CONNECTED`, `DISCONNECTED`, `MARKED_AS_DUPLICATE`,
  `UNMARKED_AS_DUPLICATE`); `MENTIONED` / `SUBSCRIBED` /
  `UNSUBSCRIBED`; auto-merge events; milestones / locking /
  pinning / transfer; deployment events. Add via the same
  `(version bump, register upgrader)` pattern when a concrete need
  arises.

## Alternatives Considered
- **Per-feature `*_synced_at` column + bespoke backfill task** —
  the prior pattern. Rejected: schema churn and parallel scans for
  every ingestion expansion. The motivation for this entire design.
- **Track `timeline_query_version` on `PullRequest`** so upgrader
  `is_complete` can avoid redundant rewalks (option (b) on Chunk
  5). Rejected at v=2 / v=3 in favor of data-migrating
  `timeline_backfill_done=False` (option (a)): bounded rewalk cost
  vs. a column whose only consumer is a single wave. Re-evaluate
  if a third correctness wave ever becomes necessary.
- **Inline-comments on `PRTimelineEvent`** (one row per inline
  comment as a synthetic timeline event). Rejected: violates the
  "1:1 with `timelineItems` nodes" invariant and conflates two
  shapes of GitHub data.
- **Bot filtering at ingest.** Rejected: lossy; query-time filtering
  is cheap and reversible.
- **Pagination for reviews with > K inline comments** done inline
  during the v=2/v=3 walks. Deferred to a v=4+ consumer of
  `PRReviewInlineCommentBackfill` so the walk's cost stays bounded.

## References
- `qb_site/syncer/services/sync_schema_upgrades.py` — registry,
  dispatcher, kick budget, target-version gate.
- `qb_site/syncer/services/sync_schema_upgrade_v2.py` /
  `sync_schema_upgrade_v3.py` — v2 / v3 upgraders.
- `qb_site/syncer/services/sub/timeline_sync.py` — event
  normalization, dismissed-review synthesis.
- `qb_site/syncer/services/sub/inline_comments_sync.py` — inline
  comment ingestion, DB-aware thread-root walk, monotone upsert.
- `qb_site/syncer/services/pr_sync_service.py` — the three timeline
  ingest call sites (bundle, forward page, back page).
- `qb_site/syncer/queries/pr_bundle.graphql` /
  `timeline_page.graphql` / `timeline_page_back.graphql` — fragment
  definitions for the new event types and nested
  `comments(first: $inlineCommentsPerReview)`.
- `qb_site/syncer/models/pull_request.py` —
  `sync_schema_version`.
- `qb_site/syncer/models/pr_timeline_event.py` — typed columns +
  CHECK constraints.
- `qb_site/syncer/models/pr_review_inline_comment.py` —
  `PRReviewInlineComment` and `PRReviewInlineCommentBackfill`.
- `qb_site/syncer/AGENTS.md` — "Timeline ingest invariants" and
  "Sync Schema Versioning" sections; checklist for new ingestion
  code.
- `docs/design-decisions/017-token-cost-tracking.md` — TaskResult
  payload conventions for GitHub-touching tasks.
