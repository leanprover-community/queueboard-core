# Sync Schema Versioning and Comment/Review Timeline Events

## Context
- The syncer currently captures only a narrow set of timeline events on
  `PRTimelineEvent` (labels, assigns, ready/draft/reopen/close, head force-pushes).
  Comments and review activity are stored only as aggregates on `PullRequest`
  (`commenters`, `approvals`, `number_total_comments`) — we have no per-event log
  of who commented, who reviewed, or when, beyond the rolled-up sets.
- We want event-level metadata (timestamps + actors + minimal type-specific fields,
  not bodies) for issue comments, review submissions/dismissals/requests, and inline
  review comments — both for richer reviewer-engagement analysis and for future
  features that need a full per-PR activity stream.
- A previous expansion of captured fields (assignees, approvals, commenters, files,
  head_ci/head_sha) introduced `engagement_synced_at` on `PullRequest` as a nullable
  timestamp + a backfill task that selects PRs where that column is null. Repeating
  this pattern — one new `*_synced_at` column per feature — does not scale: it adds
  schema churn and backfill plumbing for every future ingestion expansion.
- We need a single, durable bookkeeping mechanism that lets future ingestion
  expansions be added without further schema changes to `PullRequest`.

## Goals / Non-Goals
- Goals:
  - Persist per-event records for issue comments, review submissions/dismissals/
    requests, and inline review comments, with stable idempotency keys.
  - Replace the `engagement_synced_at` pattern with a single mechanism that scales
    to arbitrary future "we want to capture X" expansions.
  - Backfill historical events for already-synced PRs without a separate one-shot
    backfill task per feature.
  - Keep the hot-path bundle query cost roughly flat: extend existing connections
    rather than adding new top-level connections.
- Non-goals:
  - Storing comment or review bodies. Captured metadata is timestamps, actors,
    state, and minimal type-specific structure (file path, line, reply chain) only.
  - Capturing review-thread metadata (`isResolved`, thread-level resolution
    events). Threads can be approximately reconstructed from inline-comment
    `replyTo` chains; proper thread modeling is deferred to v3.
  - Pagination for reviews with more than `SYNCER_INLINE_COMMENTS_PER_REVIEW`
    inline comments. Such reviews get a row in a dedicated
    `PRReviewInlineCommentBackfill` table at ingest time so v3 can find them
    via a tiny scan; full pagination of the long tail is deferred to v3.
  - Bot filtering at ingestion. Store everything; filter at query time if needed.

## Proposed Design

### 1. `sync_schema_version` on `PullRequest`
- Add `sync_schema_version: PositiveSmallIntegerField(default=0, db_index=True)`
  to `PullRequest`.
- Add a module-level constant `CURRENT_SYNC_SCHEMA_VERSION` (initially `1`,
  bumped to `2` when this design lands).
- A single backfill task selects
  `PullRequest.objects.filter(sync_schema_version__lt=CURRENT_SYNC_SCHEMA_VERSION)`
  and dispatches to per-version upgraders from a registry. **The upgrader registry
  — not `PRSyncService` — is the sole place that advances `sync_schema_version`.**
  Each upgrader exposes:
  ```python
  class SchemaUpgrade(Protocol):
      version: int  # the target version (e.g. 2)
      def is_complete(self, pr: PullRequest) -> bool: ...
      def kick(self, pr: PullRequest) -> None: ...   # queues whatever work is needed
  ```
  The dispatcher per PR walks `version+1..CURRENT`:
  - If `is_complete(pr)` returns True for step `s`, stamp `sync_schema_version=s`
    and continue.
  - Else call `kick(pr)` and stop iterating for this PR; the next dispatcher pass
    re-checks completion.
  - Stamping happens in a single `update_fields=["sync_schema_version"]` write —
    no races against `PRSyncService` because the sync service never touches this
    column.
- Adding a future ingestion expansion is then: bump the constant, register an
  upgrader. No new column on `PullRequest`.

### 2. `engagement_synced_at` deprecation
- In the same migration that introduces `sync_schema_version`, data-migrate
  `engagement_synced_at IS NOT NULL` rows to `sync_schema_version=1`.
- `engagement_synced_at` continues to be written for one release for safety
  (rollback insurance). After one release with `sync_schema_version` proven in
  production, drop the column and the `head_ci_state IS NULL` / `head_sha`
  clauses from `backfill_repo_engagement_task` (or remove that task entirely,
  subsumed by the version-driven backfill).

### 3. New event types on `PRTimelineEvent`
- The existing discriminator column on `PRTimelineEvent` is named `type` (not
  `event_type`). New values added to that enum, all sourced from GraphQL
  `timelineItems`.
- New typed columns added to `PRTimelineEvent`:
  - `extra: JSONField(default=dict, blank=True)` — display-time
    denormalization. Read with each row, not filtered on.
  - `requested_reviewer_login: CharField(max_length=255, null=True, blank=True, db_index=True)`
    — populated for `REVIEW_REQUESTED` / `REVIEW_REQUEST_REMOVED` when the
    target is a `User` or `Mannequin`. Indexed because reviewer-engagement
    queries filter on this.
  - `requested_team_slug: CharField(max_length=255, null=True, blank=True, db_index=True)`
    — populated for the same two events when the target is a `Team`. Mutually
    exclusive with `requested_reviewer_login`.
  - `inline_comment_total_count: IntegerField(null=True, blank=True)` —
    captured from GraphQL `comments.totalCount` on `PullRequestReview`. Real
    GitHub-truth field, not sync-state. Used both for analytics
    ("how many inline comments did this review get?") and as the basis for
    detecting reviews with more inline comments than we captured.
- Existing typed columns (`label_name`, `assignee_login`, `before_sha`,
  `after_sha`) remain in place; migration to `extra` is a separate cleanup
  not in scope here.
- New `type` values:

  | `type`                      | Source GraphQL type                   | `actor_login`  | `occurred_at`     | Other typed columns                                                       | `extra`                                                                              |
  | --------------------------- | ------------------------------------- | -------------- | ----------------- | ------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ |
  | `ISSUE_COMMENTED`           | `IssueComment`                        | comment author | `createdAt`       | —                                                                         | `{}`                                                                                  |
  | `REVIEW_APPROVED`           | `PullRequestReview` (state=APPROVED)  | review author  | `submittedAt`     | `inline_comment_total_count`                                              | `{}`                                                                                  |
  | `REVIEW_CHANGES_REQUESTED`  | `PullRequestReview`                   | review author  | `submittedAt`     | `inline_comment_total_count`                                              | `{}`                                                                                  |
  | `REVIEW_COMMENTED`          | `PullRequestReview` (state=COMMENTED) | review author  | `submittedAt`     | `inline_comment_total_count`                                              | `{}`                                                                                  |
  | `REVIEW_DISMISSED`          | `ReviewDismissedEvent`                | dismisser      | event `createdAt` | —                                                                         | `{"dismissed_review_node_id": "...", "dismissed_review_author": "alice", "dismissed_review_submitted_at": "...", "previous_review_state": "APPROVED"}` |
  | `REVIEW_REQUESTED`          | `ReviewRequestedEvent`                | requester      | event `createdAt` | one of `requested_reviewer_login` / `requested_team_slug`                 | `{}`                                                                                  |
  | `REVIEW_REQUEST_REMOVED`    | `ReviewRequestRemovedEvent`           | remover        | event `createdAt` | one of `requested_reviewer_login` / `requested_team_slug`                 | `{}`                                                                                  |

- The `review_state` is *not* denormalized into `extra` — it's already encoded
  by the `type` value (`REVIEW_APPROVED` vs `REVIEW_CHANGES_REQUESTED` vs
  `REVIEW_COMMENTED`).
- The "inline comments incomplete" marker is *not* stored on `PRTimelineEvent`.
  Instead, see §4 — incomplete reviews get a row in
  `PRReviewInlineCommentBackfill` so the v3 recovery scan can find them in
  O(rows-needing-work) rather than O(reviews).
- **Invariant:** every `PRTimelineEvent` row corresponds 1:1 to a node from
  GitHub's `timelineItems` connection. Inline review comments — which live
  nested under `PullRequestReview.comments`, not in `timelineItems` — go in a
  separate model (next section).
- Idempotency continues via unique `github_node_id`.

### 4. New models: `PRReviewInlineComment` and `PRReviewInlineCommentBackfill`

#### 4a. `PRReviewInlineComment`
- Mirrors GraphQL's `PullRequestReviewComment`. One row per inline comment.
  ```python
  class PRReviewInlineComment(models.Model):
      pull_request = FK(PullRequest, related_name="review_inline_comments", on_delete=CASCADE)
      parent_review_event = FK(PRTimelineEvent, null=True, on_delete=SET_NULL,
                               related_name="inline_comments")
      github_node_id = CharField(max_length=64, unique=True)
      review_node_id = CharField(max_length=64, db_index=True)  # PullRequestReview.id
      author_login = CharField(max_length=64, blank=True)
      created_at = DateTimeField()
      path = CharField(max_length=512)
      line = IntegerField(null=True, blank=True)
      original_line = IntegerField(null=True, blank=True)
      reply_to_node_id = CharField(max_length=64, null=True, blank=True)
      thread_root_node_id = CharField(max_length=64, db_index=True)

      class Meta:
          indexes = [models.Index(fields=["pull_request", "created_at"])]
  ```
- `parent_review_event` is the FK to the `PRTimelineEvent` row for the enclosing
  `PullRequestReview` submission. Nullable because the parent event row may be
  recreated; `review_node_id` is the durable link.
- `thread_root_node_id` is the node id at the top of the `replyTo` chain (or the
  comment's own id if it is itself a thread root). Computed at ingest by walking
  `replyTo` within the in-flight set; comments whose `replyTo` target is outside
  the current bundle fall back to `reply_to_node_id` as the root (best effort,
  reconciled on subsequent rewalks).
- Idempotency via unique `github_node_id`. Inserts use
  `bulk_create(..., ignore_conflicts=True)`.
- Captured in v2 via the same timeline rewalk that captures the parent reviews —
  no separate pagination state on `PullRequest` is needed.

#### 4b. `PRReviewInlineCommentBackfill` (sync-state, deferred consumer in v3)
- Tracks reviews where our nested `comments(first: K)` fetch hit the page limit
  and we know we missed the long tail. Consumed by a v3 recovery sweep that
  paginates the rest. Empty for reviews that fit in `K`.
  ```python
  class PRReviewInlineCommentBackfill(models.Model):
      review_event = OneToOneField(PRTimelineEvent, on_delete=CASCADE, primary_key=True)
      pull_request = ForeignKey(PullRequest, on_delete=CASCADE)
      review_node_id = CharField(max_length=64, db_index=True)
      total_count = IntegerField()  # snapshot of comments.totalCount at ingest
      created_at = DateTimeField(auto_now_add=True)
      # cursor / last_attempt_at fields land in v3 alongside the paginator.
  ```
- Written by the syncer when a `PullRequestReview`'s nested `comments`
  connection has `pageInfo.hasNextPage = true`. Idempotent insert
  (`primary_key` is the parent review event; second ingest of the same review
  is a no-op).
- The v3 recovery scan is `SELECT ... FROM PRReviewInlineCommentBackfill LIMIT N`
  — the table *is* the index, so the scan is sub-millisecond regardless of how
  many millions of `PRTimelineEvent` rows exist.
- Rows are deleted (or marked complete) by the v3 paginator once it has fetched
  all remaining inline comments for the review. Until then, this row is the
  durable signal that the review is incomplete; `inline_comment_total_count` on
  the parent event row records the GitHub-truth count for analytics.
- This pattern keeps sync-state ("we still owe inline comments for this review")
  on a separate, small table rather than mixing it into `PRTimelineEvent`. Same
  pattern can be reused by future deferred-pagination work without growing
  `PRTimelineEvent`.

### 5. GraphQL extensions
- Extend `qb_site/syncer/queries/pr_bundle.graphql` `timelineItems`:
  - Add to `itemTypes`: `ISSUE_COMMENT`, `PULL_REQUEST_REVIEW`,
    `REVIEW_DISMISSED_EVENT`, `REVIEW_REQUESTED_EVENT`,
    `REVIEW_REQUEST_REMOVED_EVENT`.
  - Add per-type fragments. The `PullRequestReview` fragment includes a nested
    `comments(first: K)` connection so we capture inline comments at the same
    time as the review submission:
    ```graphql
    ... on IssueComment { id  createdAt  author { login } }
    ... on PullRequestReview {
      id
      submittedAt
      state
      author { login }
      comments(first: 20) {
        nodes {
          id
          createdAt
          path
          line
          originalLine
          replyTo { id }
          author { login }
        }
        pageInfo { hasNextPage }
        totalCount
      }
    }
    ... on ReviewDismissedEvent {
      id  createdAt  actor { login }  previousReviewState
      review { id  submittedAt  author { login } }
    }
    ... on ReviewRequestedEvent {
      id  createdAt  actor { login }
      requestedReviewer {
        ... on User { login }
        ... on Team { slug }
        ... on Mannequin { login }
      }
    }
    ... on ReviewRequestRemovedEvent { /* same shape as ReviewRequestedEvent */ }
    ```
- Apply the same fragment additions to `timeline_page.graphql` and
  `timeline_page_back.graphql` so backfill paging surfaces the same types.
- The `comments(first: K)` value comes from a setting
  `SYNCER_INLINE_COMMENTS_PER_REVIEW` (default `20`). When
  `pageInfo.hasNextPage=true` on a review's comments connection, the syncer
  inserts a `PRReviewInlineCommentBackfill` row for that review so v3 can find
  the outliers via a tiny dedicated-table scan (see §4b). The parent review
  event's `inline_comment_total_count` column records `comments.totalCount`
  unconditionally (truth from GitHub), regardless of completeness.

### 6. v2 upgrader: reset timeline backfill for already-done PRs
- For PRs at `sync_schema_version < 2`, the v2 upgrader's `kick(pr)`:
  1. If `timeline_backfill_done=True`, set it to `False` and clear
     `timeline_backfill_cursor` so the existing
     `backfill_repo_incomplete_prs` task re-walks history with the new
     `itemTypes` and the nested `comments(first: K)` fetch.
  2. Enqueue `sync_pr_task(force=True)` so dedupe doesn't swallow the rewalk.
- The v2 upgrader's `is_complete(pr)` returns `pr.timeline_backfill_done` —
  i.e., the timeline has been fully walked. Because the upgrader's `kick` is the
  only thing that flips `done` back to False under v2 code, a True observation
  after the kick implies the rewalk happened.
- Re-walking is safe and cheap relative to alternatives:
  - `github_node_id` uniqueness makes re-inserting v1-era events a no-op.
  - The new query requests the additional itemTypes plus the existing v1 types
    in the same paginated walk (no extra round trip per page).
- For PRs that were never timeline-backfilled at v1
  (`timeline_backfill_done=False`), no reset is required — the next backfill
  pass picks up the new types and inline comments automatically; the upgrader
  just stamps once `is_complete` returns True.

## Subtleties / Invariants
- **`sync_schema_version` is owned by the upgrader registry.** `PRSyncService`
  does not write it. This avoids a bug where a sync that succeeds without the
  upgrader having run would falsely advance the version (e.g., a v=1 PR whose
  bundle sync completes before its v2 timeline rewalk would otherwise be
  prematurely stamped to 2).
- **`PRTimelineEvent` rows correspond 1:1 to `timelineItems` nodes.** Inline
  review comments live in `PRReviewInlineComment`, not in `PRTimelineEvent`.
  This invariant keeps future expansions easy to reason about and avoids
  confusion about the source of any given event row.
- **`REVIEW_DISMISSED` actor ≠ review author.** Always populate `actor_login`
  from `ReviewDismissedEvent.actor`. The dismissed review's identity, author,
  and submission timestamp are *denormalized* into
  `extra.dismissed_review_node_id`, `extra.dismissed_review_author`, and
  `extra.dismissed_review_submitted_at` so the row is self-contained and
  remains interpretable even if the original review event predates the timeline
  window we've walked so far.
- **`REVIEW_COMMENTED` semantics.** Each `PullRequestReview` becomes one
  `REVIEW_*` event regardless of how many inline comments it contains. The
  inline comments live in `PRReviewInlineComment` rows linked via
  `review_node_id` / `parent_review_event`. A reply to an existing thread (in
  modern GitHub) is also wrapped in a one-comment `PullRequestReview` with
  `state=COMMENTED`, so it flows through the same path: one
  `REVIEW_COMMENTED` event row + one `PRReviewInlineComment` row.
- **Pending reviews are dropped at ingest.** Pending reviews (drafted but not
  submitted) have `submittedAt=null` and aren't real events yet. They will
  appear later with a non-null `submittedAt` once submitted.
- **Mannequins and deleted accounts.** `author`/`actor` may be `null` (deleted
  account) or a `Mannequin`. Persist `actor_login` as `""` when the GraphQL
  value is `null`; use the mannequin login when present. Verify the column
  shape on `PRTimelineEvent.actor_login` and `PRReviewInlineComment.author_login`
  at implementation time and align convention (likely empty string given
  existing usage).
- **Team review requests.** `requestedReviewer` can be a `User`, `Team`, or
  `Mannequin`. Use `requested_reviewer_login` (typed column) for users and
  mannequins, `requested_team_slug` (typed column) for teams; the two are
  mutually exclusive. Both columns are indexed because reviewer-engagement
  queries filter on them.
- **Idempotency on insert.** Always `bulk_create(..., ignore_conflicts=True)`
  keyed on `github_node_id`. **Persist events first, advance the timeline
  cursor second** — a crash between the two leaves a safe re-fetch state on
  retry. Same for `PRReviewInlineComment`: persist the inline comments before
  advancing the cursor that brought us their parent reviews.
- **`force=True` for upgrader-triggered syncs.** Existing `sync_pr_task` has
  runtime + enqueue dedupe with 300s TTL. The upgrader must pass `force=True`
  (or use a distinct dedupe namespace) to ensure the rewalk runs even when the
  PR was recently synced.
- **Soft-deprecation of `approvals` / `commenters` / `number_total_comments`.**
  The bundle continues to compute these from the `reviews(first: 100)` and
  `comments(first: 100)` connections at v2 — no consumer churn. Mark them
  "soft-deprecated, prefer event log" in code comments and plan a v3 that
  switches computation to the event log. Until then, accept that the >100
  boundary may show inconsistencies between the aggregate fields and the
  event-log totals.
- **No bot filtering at ingestion.** All events are stored; downstream filters
  by actor login as needed.
- **Scan-performance posture for new periodic tasks.** Every recurring scan
  introduced or replaced by this design uses an indexed predicate that returns
  O(rows-needing-work), not O(table-size). This is deliberate — the prior
  `engagement_synced_at` scan was unindexed and degraded as the table grew.
  Specifically:
  - Upgrader dispatch: `WHERE sync_schema_version < CURRENT` against an
    indexed `PositiveSmallIntegerField`. After a wave completes, almost all
    rows are at `CURRENT`, the planner sees ~0% selectivity, and the index
    scan returns immediately. Postgres 13+ B-tree deduplication keeps the
    index small even with heavy skew. We rely on the regular B-tree rather
    than a partial index `WHERE sync_schema_version < <const>` because the
    constant would have to migrate on every version bump, with negligible
    extra performance to show for it. Verified post-deploy via
    `EXPLAIN ANALYZE` (see Validation Plan).
  - Engagement backfill (existing, unindexed multi-OR): subsumed and removed
    in Chunk 6.
  - Inline-comment recovery (deferred to v3 consumer): scans
    `PRReviewInlineCommentBackfill`, where the table itself is the index of
    "needs work" — sub-millisecond regardless of total `PRTimelineEvent` size.
  - Reviewer-engagement queries: `requested_reviewer_login` and
    `requested_team_slug` are indexed columns on `PRTimelineEvent`. Querying
    "PRs where X was requested in window W" is a typical (column, occurred_at)
    range scan.

## Timeline event types deliberately NOT captured at v2
For future readers wondering "should we add X?": the following `timelineItems`
types are intentionally out of scope at v2. We can add any of them in a future
schema version if a concrete need arises.

- State change neighbors of CLOSED/REOPENED: `MERGED` (close cousin of CLOSED;
  worth strong consideration in v3).
- PR identity changes: `RENAMED_TITLE`, `BASE_REF_CHANGED`,
  `BASE_REF_FORCE_PUSHED`.
- Cross-issue/PR linkage: `CROSS_REFERENCED`, `REFERENCED`, `CONNECTED`,
  `DISCONNECTED`, `MARKED_AS_DUPLICATE`, `UNMARKED_AS_DUPLICATE`.
- Mentions / subscriptions: `MENTIONED`, `SUBSCRIBED`, `UNSUBSCRIBED`.
- Auto-merge / automatic base change: `AUTO_MERGE_ENABLED`,
  `AUTO_MERGE_DISABLED`, `AUTOMATIC_BASE_CHANGE_FAILED`,
  `AUTOMATIC_BASE_CHANGE_SUCCEEDED`.
- Milestones / locking / pinning / transfer: `MILESTONED`, `DEMILESTONED`,
  `LOCKED`, `UNLOCKED`, `PINNED`, `UNPINNED`, `TRANSFERRED`, `USER_BLOCKED`.
- Deployments: `DEPLOYED`, `DEPLOYMENT_ENVIRONMENT_CHANGED`.

## Implementation Plan (Chunks)
1. **Schema scaffold.**
   - Add `PullRequest.sync_schema_version` (`PositiveSmallIntegerField`,
     default=0, db_index=True) and `CURRENT_SYNC_SCHEMA_VERSION=1` constant in
     `qb_site/syncer/services/sync_schema_upgrades.py` (stub file; framework
     lands in Chunk 2).
   - Add to `PRTimelineEvent`:
     - `extra` JSONField (default=dict, blank=True),
     - `inline_comment_total_count` IntegerField (null/blank),
     - `requested_reviewer_login` CharField (null/blank, db_index=True),
     - `requested_team_slug` CharField (null/blank, db_index=True).
   - Data migration: stamp `sync_schema_version=1` where
     `engagement_synced_at IS NOT NULL`.
   - Update `scripts/backup_policy.py` if the new fields/tables need
     additional handling (and, in chunk 3, the new models).
   - Update root `AGENTS.md` and `qb_site/syncer/AGENTS.md` to reference the
     upgrader framework, the new task, and the new models.
   - No behavior change yet (constant still 1; upgrader registry is empty).
2. **Upgrader framework.**
   - Add `qb_site/syncer/services/sync_schema_upgrades.py` with the registry,
     dispatcher, and per-version `SchemaUpgrade` protocol.
   - Add a Celery task `syncer.upgrade_schema_versions` that selects PRs where
     `sync_schema_version < CURRENT_SYNC_SCHEMA_VERSION` and dispatches
     upgraders with the same dedupe/rate-limit treatment as existing backfill
     tasks.
   - Add `syncer.upgrade_schema_versions_active_task` periodic beat entry.
3. **`PRReviewInlineComment` and `PRReviewInlineCommentBackfill` models + ingestion path.**
   - New model migrations for both tables.
   - Update `scripts/backup_policy.py` for the two new tables.
   - Add ingestion in `PRSyncService` to translate nested
     `PullRequestReview.comments` into `PRReviewInlineComment` rows linked via
     `review_node_id` and (when available) `parent_review_event`. Compute
     `thread_root_node_id` from the `replyTo` chain within the in-flight set;
     fall back to `reply_to_node_id` as root if the target is outside the
     bundle.
   - When `pageInfo.hasNextPage=true` on a review's nested `comments`
     connection, insert a `PRReviewInlineCommentBackfill` row for that review.
     Idempotent (primary_key is the parent review event).
   - Always populate `PRTimelineEvent.inline_comment_total_count` from
     `comments.totalCount`.
4. **GraphQL + new timeline event types.**
   - Extend `pr_bundle.graphql`, `timeline_page.graphql`,
     `timeline_page_back.graphql` with new `itemTypes` and per-type fragments,
     including the nested `comments(first: SYNCER_INLINE_COMMENTS_PER_REVIEW)`
     on `PullRequestReview`.
   - Extend the timeline-event normalizer to translate the new GraphQL nodes
     into `PRTimelineEvent` rows; populate `inline_comment_total_count` from
     `comments.totalCount` and route `requestedReviewer` to either
     `requested_reviewer_login` or `requested_team_slug` depending on the
     GraphQL union member.
   - Add `type` enum entries on `PRTimelineEventType`.
   - Measure bundle payload growth on a representative busy mathlib4 PR and
     tune `last:` (currently 250) downward if needed; document the chosen
     value.
5. **v2 upgrader.**
   - Implement `upgrade_to_v2` (`is_complete`, `kick`); register at version 2.
   - Bump `CURRENT_SYNC_SCHEMA_VERSION = 2`.
   - Settings gate: `SYNCER_SCHEMA_UPGRADE_TARGET_VERSION` (default
     `CURRENT_SYNC_SCHEMA_VERSION`) so we can advance the wave deliberately on
     deploy.
   - `kick(pr)` enqueues `sync_pr_task(force=True)`.
6. **`engagement_synced_at` deprecation (one release later).**
   - Stop writing `engagement_synced_at` and remove its filter clauses from
     `backfill_repo_engagement_task`.
   - Migration to drop the column.

## Validation Plan
- **Unit / integration tests:**
  - Extend `qb_site/syncer/tests/fixtures/` with a bundle fixture that includes
    the new `timelineItems` types and at least one review with multiple inline
    comments, including a thread reply
    (`pr_bundle_with_engagement_events.json`).
  - New tests under `qb_site/syncer/tests/`:
    - `test_sync_schema_version.py`: stamping is performed only by the upgrader
      registry, never by `PRSyncService`; data-migration mapping from
      `engagement_synced_at`; idempotent upgrader dispatch; partial-failure
      resume.
    - `test_engagement_event_ingestion.py`: each new event type maps to the
      correct row with correct `actor_login`/`occurred_at`/typed columns/`extra`;
      bot actors stored, not filtered; null/mannequin actors handled; pending
      reviews dropped; `REVIEW_DISMISSED` actor distinct from reviewer; team
      requested-reviewer routed to `requested_team_slug`, user/mannequin
      requested-reviewer routed to `requested_reviewer_login`;
      `inline_comment_total_count` populated from `comments.totalCount`.
    - `test_review_inline_comments.py`: inline comments mapped from
      `PullRequestReview.comments` to `PRReviewInlineComment`;
      `thread_root_node_id` computed from `replyTo` chain;
      `parent_review_event` FK populated;
      `PRReviewInlineCommentBackfill` row inserted iff
      `pageInfo.hasNextPage=true`; insert is idempotent on re-ingest;
      `bulk_create(ignore_conflicts=True)` no-ops on re-ingest of inline
      comments.
    - `test_v2_upgrader.py`: reset of `timeline_backfill_done`; `force=True`
      passthrough; idempotent under retries; `is_complete` returns True only
      after rewalk.
- **Manual checks:**
  - Pick a high-engagement mathlib4 PR (>50 reviews, >100 issue comments,
    multiple review threads with replies) and confirm post-upgrade event counts
    match GitHub's UI; spot-check inline comments on a known thread.
  - Confirm `sync_schema_version` advances 1 → 2 on a representative sample
    after an upgrader pass.
  - Watch token rate-limit budget during the upgrade wave; confirm
    `SYNCER_RATE_REMAINING_MIN` deferral behaves as on existing backfills.
  - Inspect bundle payload size before/after on a busy PR.
- **Scan-performance check:** during the v=2 wave on real data, run
  `EXPLAIN ANALYZE SELECT id FROM syncer_pullrequest WHERE sync_schema_version < 2 LIMIT 50;`
  and confirm the plan is either an Index Scan on the
  `sync_schema_version` index or a Seq Scan whose work is bounded by the
  `LIMIT` (i.e., it stops once it has 50 rows). A Seq Scan that reads the
  full table to satisfy `LIMIT` is the regression mode and should be
  investigated before continuing the wave.
- **Repo checks:** `bash scripts/repo_check_compose.sh` (covers ruff, Django
  tests, sanitized-backup policy — `sync_schema_version`, `extra`, the new
  typed columns on `PRTimelineEvent`, and the new
  `PRReviewInlineComment` / `PRReviewInlineCommentBackfill` tables must all
  be reflected in `scripts/backup_policy.py`).

## Open Questions (settle during implementation)
- Cost of the nested `comments(first: K)` fetch on `PullRequestReview` within
  `timelineItems(last: 250)` on busy PRs. Measure payload size and rate-limit
  cost on a representative mathlib4 PR before settling on `K` and `last:`.
- Should the v2 upgrader live as its own Celery task or be folded into
  `backfill_repo_incomplete_prs`? Folding is simpler; separate gives cleaner
  observability per upgrade wave.
- Whether to keep writing `engagement_synced_at` across the deprecation window
  for rollback insurance, or stop writing it immediately and rely on
  `sync_schema_version >= 1`.
- Convention for `actor_login` / `author_login` when GraphQL author/actor is
  null — verify against the existing `CharField` definitions (nullable vs.
  blank-default-empty).
- `MERGED` event in v3: bundled with whatever the next expansion is, or its
  own version bump?

## Progress Notes
- 2026-05-07: Initial design drafted.
- 2026-05-07: Refined to capture inline review comments via nested
  `PullRequestReview.comments` rather than punting them to v3; introduced
  `PRReviewInlineComment` model rather than synthesizing inline-comment rows
  in `PRTimelineEvent` (preserves the "1 row ↔ 1 timelineItems node"
  invariant).
- 2026-05-07: Stamping rule moved entirely to the upgrader registry; explicit
  invariants added for `force=True`, soft-deprecation of aggregates, payload
  growth, mannequins, team review requests, and the deliberate non-capture
  list.
- 2026-05-07: Promoted reviewer-engagement query fields to typed columns
  (`requested_reviewer_login`, `requested_team_slug`) on `PRTimelineEvent`
  rather than burying them in `extra`. Replaced the
  `inline_comments_incomplete` boolean (originally in `extra`) with a
  combination of `inline_comment_total_count` (GitHub-truth, on
  `PRTimelineEvent`) and a new dedicated `PRReviewInlineCommentBackfill`
  table — separating sync-state from event data and keeping the v3 recovery
  scan O(rows-needing-work). Also dropped the redundant `review_state` from
  `extra` (the `type` discriminator already encodes it) and renamed
  `event_type` → `type` throughout to match the existing column name on
  `PRTimelineEvent`. Added scan-performance subtlety covering all new and
  replaced periodic scans.

## Finalization Notes
- After v2 ships and `engagement_synced_at` is dropped, convert this doc into
  a concise final-decision record describing: the upgrader framework, the set
  of event types captured at v2, `PRReviewInlineComment`, and the deprecated
  mechanism. Move chunked rollout details to git history.
