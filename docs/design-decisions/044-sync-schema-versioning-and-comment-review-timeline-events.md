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

## Implementation Status (as of 2026-05-08)
| Chunk | State | Notes |
| ----- | ----- | ----- |
| 1. Schema scaffold | **Deployed** | `PullRequest.sync_schema_version` + four new typed columns on `PRTimelineEvent`. Migration `0039` ran cleanly; data migration stamped existing engagement-synced PRs to v=1. |
| 2. Upgrader framework + convergence metric | **Deployed** | Periodic task `syncer.upgrade_schema_versions` is firing; convergence canary reports `prs_below_current_sync_schema_version=0` and `sync_schema_version_target=1` per repo. Registry empty as designed; auto-stamp path is what's running. **Until Chunk 5 bumps `CURRENT_SYNC_SCHEMA_VERSION` to 2, this task has nothing to do** — every PR is already at the target. |
| 3a. Inline-comment models + admin + backup policy | **Committed, awaiting deploy** | Pure additive migration `0041`; both new tables and admins ready; `validate_backup_policy.py` updated. |
| 3b. Inline-comment ingestion service | **Committed, awaiting deploy** | `qb_site/syncer/services/sub/inline_comments_sync.py` + tests. Service was importable but unreferenced before 4c. |
| 4a. GraphQL fragments + setting | **Committed, awaiting deploy** | Fragments added to `pr_bundle.graphql`, `timeline_page.graphql`, `timeline_page_back.graphql`; `$inlineCommentsPerReview` threaded; `SYNCER_INLINE_COMMENTS_PER_REVIEW` (default 20) added; `validate_github_graphql.py` updated. |
| 4b. Normalizer + REVIEW_*/ISSUE_COMMENTED ingestion | **Committed, awaiting deploy** | 7 new `PRTimelineEventType` values + metadata-only migration `0042`; `timeline_sync.py` extended to map new `__typename`s with state-routing for reviews, pending-review drop, dismissed-review null-guard, requestedReviewer routing (User/Bot/Mannequin → login; Team → slug), and `inline_comment_total_count` refresh. New test class with 16 cases. |
| 4c. Wire inline-comments service | **Committed, awaiting deploy** | `PRSyncService.sync_pull_request_bundle` now collects review nodes and calls `sync_review_inline_comments_bundle` once per bundle. Includes `state=DISMISSED` reviews' inline comments (verified live: those nodes appear in `timelineItems` with non-null `submittedAt`). End-to-end fixture + 9-case integration test. |
| 4d. Strict CHECK constraints | **Next** | Adds CHECK constraints on `requested_reviewer_login`, `requested_team_slug`, `inline_comment_total_count` mirroring the existing `syncer_prtl_label_by_type_ck` / `syncer_prtl_sha_by_type_ck` pattern. Per design, lands at least one deploy after 4b/4c so any unexpected GraphQL shape surfaces as a test/staging failure rather than an ingestion crash. |
| 5. v2 upgrader (the wave) | Pending | Bumps `CURRENT_SYNC_SCHEMA_VERSION = 2` AND registers `upgrade_to_v2`. Until both ship together, the existing `syncer.upgrade_schema_versions` task is a no-op (target=1, every PR already there). After Chunk 5 deploys, the convergence canary spikes and trends back to 0 over days as the wave converges. |
| 6. `engagement_synced_at` deprecation | Pending | Post-soak after Chunk 5. |

### Resumption pointer for the next agent
The branch `sync-schema-versioning` carries Chunks 3a, 3b, 4a, 4b, 4c
ready for deploy. Chunks 1+2 are in production. The next units of work,
in order:

1. **Chunk 4d — strict `CHECK` constraints** (small migration + a test).
   Concrete constraint definitions are in §Chunk 4d below; routing
   decisions from Phase 0 are baked in (Bot reviewers → `requested_reviewer_login`).
   Should land *after* a soak deploy of 4a–4c so any production-data
   shape mismatch surfaces as test failures, not ingestion crashes.
2. **Chunk 5 — v2 upgrader (the wave).** This is where the historical
   backfill of `IssueComment` / `PullRequestReview` events on
   already-fully-walked PRs actually runs. **Read §Chunk 5's
   "Correctness pitfall" callout first** — there's a real bug in the
   original `is_complete(pr) := pr.timeline_backfill_done` rule that
   needs to be addressed (PRs with `timeline_backfill_done=True` from
   v1-era walks would otherwise be stamped to v=2 without ever
   capturing historical events).
3. **Chunk 6 — drop `engagement_synced_at`.** Trivial, post-soak.

If the post-deploy of 4a–4c surfaces an unexpected GraphQL shape
(e.g. a previously-unobserved `requestedReviewer` union member, or an
edge case Phase 0 missed), encode the chosen routing in 4d's CHECK
constraint definitions before they land.

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
  - **No upgrader registered for step `s`** → auto-stamp `sync_schema_version=s`
    and continue. This intentionally skips trivial / already-baseline versions
    (e.g. v=1, which corresponds to engagement fields that `PRSyncService`
    already writes on every sync) without forcing the registration of a
    no-op upgrader. See Subtleties for the trade-off.
  - **Upgrader registered and `is_complete(pr)` returns True** → stamp
    `sync_schema_version=s` and continue.
  - **Otherwise** → call `kick(pr)` and stop iterating for this PR; the next
    dispatcher pass re-checks completion.
  - Stamping uses a guarded `update(...)` keyed on `pk` and a
    `sync_schema_version__lt=s` predicate, so concurrent dispatchers can't
    walk the column backward.
- Adding a future ingestion expansion is then: bump the constant, register an
  upgrader (or skip registration if the new version's data is implicitly
  already-captured by the existing sync path, in which case auto-stamp does
  the right thing). No new column on `PullRequest`.
- Per-task **kick budget** separates the two workloads inside a single task
  invocation: stamping is DB-only and can clear large backlogs cheaply, while
  `kick` enqueues GitHub-bound work and must be paced. The dispatcher accepts
  a `kick_budget`; once exhausted it stops emitting kicks (but continues
  stamping). The two budgets are configured via
  `SYNCER_SCHEMA_UPGRADE_BATCH_SIZE` (default 1000, max PRs considered per
  invocation) and `SYNCER_SCHEMA_UPGRADE_KICK_LIMIT` (default 20, max kicks
  per invocation, sized to mirror the engagement-backfill pacing).

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
- **Missing-upgrader auto-stamp is deliberate.** A version step with no
  registered upgrader is treated as "satisfied by default" by the dispatcher,
  which stamps and continues. This is the right behavior for v=1 — the
  engagement fields it tracks are already written on every `PRSyncService`
  sync, so a "v1 upgrader" would be a stamper-only class with a `kick` path
  that is never exercised in practice. Coding that explicitly is busywork.
  The trade-off is that a future PR that bumps `CURRENT_SYNC_SCHEMA_VERSION`
  and *forgets* to register the upgrader will silently auto-stamp PRs through
  the new version. Mitigation: (1) the version-bump diff has to add both the
  constant and the registration, so reviewers see them together; (2) the
  dispatcher logs an info-level event each time it auto-stamps, so missing
  registrations show up in operational logs.
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
Chunks 1, 2, 3a, 3b, 4a, 4b, 4c are complete; details preserved in git
history and summarized in Progress Notes. The sections below describe what
remains.

### Chunk 4 — GraphQL + new timeline event types + wiring
**4a, 4b, 4c are committed and awaiting deploy.** Phase 0 verification
findings are recorded in Progress Notes (2026-05-08); the original Phase 0
query template lives in commit `24602eb` if anyone needs to re-probe.

The remaining sub-chunk is **4d** below.

#### Chunk 4d. Strict CHECK constraints (next up)
Lands at least one deploy after 4b/4c so real-world data has flowed
through the new ingestion path and any edge-case shapes Phase 0 missed
have surfaced as test/staging failures rather than ingestion crashes.

**Migration content.** Add three CHECK constraints on `PRTimelineEvent`,
named to match the existing `syncer_prtl_label_by_type_ck` /
`syncer_prtl_sha_by_type_ck` pattern. Translated to Django `Q`:

```python
# In syncer/models/pr_timeline_event.py Meta.constraints:
models.CheckConstraint(
    name="syncer_prtl_requested_reviewer_by_type_ck",
    condition=(
        Q(requested_reviewer_login__isnull=True, requested_team_slug__isnull=True)
        | Q(
            type__in=[
                PRTimelineEventType.REVIEW_REQUESTED,
                PRTimelineEventType.REVIEW_REQUEST_REMOVED,
            ]
        )
    ),
),
models.CheckConstraint(
    name="syncer_prtl_requested_reviewer_mutex_ck",
    # At most one of the two columns is non-null.
    condition=Q(requested_reviewer_login__isnull=True) | Q(requested_team_slug__isnull=True),
),
models.CheckConstraint(
    name="syncer_prtl_inline_total_by_type_ck",
    condition=(
        Q(inline_comment_total_count__isnull=True)
        | Q(
            type__in=[
                PRTimelineEventType.REVIEW_APPROVED,
                PRTimelineEventType.REVIEW_CHANGES_REQUESTED,
                PRTimelineEventType.REVIEW_COMMENTED,
            ]
        )
    ),
),
```

**Routing already encoded by 4b/4c (not a new decision):**
- `Bot` / `User` / `Mannequin` reviewers → `requested_reviewer_login`.
- `Team` reviewers → `requested_team_slug`.
- The two columns are mutually exclusive.
- `inline_comment_total_count` is set only on the three submitted
  review types; not on `REVIEW_DISMISSED` (that captures the
  `ReviewDismissedEvent`, not the review itself).

**Pre-flight checks before writing the migration:**
1. Confirm production data already conforms by running, on a recent DB
   snapshot or in a read replica:
   ```sql
   -- All non-conforming rows for each constraint should return 0.
   SELECT COUNT(*) FROM syncer_prtimelineevent
     WHERE (requested_reviewer_login IS NOT NULL OR requested_team_slug IS NOT NULL)
       AND type NOT IN ('REVIEW_REQUESTED', 'REVIEW_REQUEST_REMOVED');
   SELECT COUNT(*) FROM syncer_prtimelineevent
     WHERE requested_reviewer_login IS NOT NULL AND requested_team_slug IS NOT NULL;
   SELECT COUNT(*) FROM syncer_prtimelineevent
     WHERE inline_comment_total_count IS NOT NULL
       AND type NOT IN ('REVIEW_APPROVED', 'REVIEW_CHANGES_REQUESTED', 'REVIEW_COMMENTED');
   ```
2. If any of those queries returns rows, do not add the constraint
   until ingestion has been audited; the rows came from a code path
   we didn't expect. Update Progress Notes with the finding.

**Tests.** Add cases to
`qb_site/syncer/tests/subsystems/test_timeline_sync.py` (or a sibling
file) that attempt to create rows violating each constraint and assert
`IntegrityError`. The existing
`TestTimelineSyncReviewAndCommentEvents` already covers the happy
paths; this new file just adds the negative cases.

**Rollback:** if a constraint violates real data in production, drop
it in a hotfix migration; the data shape is the source of truth.

### Chunk 5. v2 upgrader (the wave)
This is where the **historical backfill** of `IssueComment` /
`PullRequestReview` events on already-fully-walked PRs actually
happens. After the 4a–4c soak, fresh syncs and ongoing
`backfill_repo_incomplete_prs` runs capture new event types
opportunistically, but PRs that finished their v1 timeline walk before
the deploy still have a `timeline_backfill_done=True` flag from the
v1-era query — i.e., they were "fully walked" under the narrower
v1 `itemTypes` and never re-walked under the v2 fragments. Chunk 5
forces those rewalks.

#### Correctness pitfall to address (raised 2026-05-08)
The original design had `is_complete(pr) := pr.timeline_backfill_done`.
That's wrong on its own: a v1-era walk that left
`timeline_backfill_done=True` would make `is_complete` short-circuit
to True at v=2, the dispatcher would stamp the PR to v=2 without
calling `kick`, and the historical event-type backfill would be
silently skipped.

Pick one of these two fixes when implementing Chunk 5:

- **(a) Force-reset on deploy (simplest, slightly wasteful).** In the
  same migration that bumps `CURRENT_SYNC_SCHEMA_VERSION = 2`,
  data-migrate `timeline_backfill_done=False` and
  `timeline_backfill_cursor=NULL` for every PR with
  `sync_schema_version < 2`. Now every PR starts from a known state.
  PRs that already got post-4a rewalks via
  `backfill_repo_incomplete_prs` get re-rewalked once — wasted work
  but bounded by `SYNCER_SCHEMA_UPGRADE_KICK_LIMIT`.
- **(b) Track the query version (more precise).** Add a column
  `PullRequest.timeline_query_version: PositiveSmallIntegerField(default=0)`,
  updated by `PRSyncService` to a module-level
  `CURRENT_TIMELINE_QUERY_VERSION` whenever a full-history walk
  completes (`hasPreviousPage=False` on the timeline page). At v=2,
  that constant is `2`. Then
  `is_complete(pr) := pr.timeline_backfill_done and pr.timeline_query_version >= 2`.
  Avoids the redundant rewalks. **Important:** writing
  `timeline_query_version` is NOT the same as writing
  `sync_schema_version` — the former is "what query did we walk
  with", the latter is "what ingestion expansion has been satisfied
  for this PR" and is still owned exclusively by the upgrader
  registry per the Subtleties invariant.

Pick **(a)** unless the redundant-work cost looks meaningful at our
scale (it likely doesn't). Document the choice in Progress Notes.

#### Concrete plan
- Register `upgrade_to_v2` at version 2 in
  `qb_site/syncer/services/sync_schema_upgrades.py`:
  - `is_complete(pr)`: see the pitfall above. Either
    `pr.timeline_backfill_done` (paired with the data-migration
    reset in option (a)) or
    `pr.timeline_backfill_done and pr.timeline_query_version >= 2`
    (option (b)).
  - `kick(pr)`:
    1. Set `timeline_backfill_done=False` and clear
       `timeline_backfill_cursor` so
       `backfill_repo_incomplete_prs` re-walks history with the v2
       `itemTypes` and the nested `comments(first: K)` fetch.
    2. Enqueue `sync_pr_task(force=True)` so the runtime/enqueue
       dedupe (300s TTL) doesn't swallow the rewalk.
- Bump `CURRENT_SYNC_SCHEMA_VERSION = 2`.
- Add settings gate `SYNCER_SCHEMA_UPGRADE_TARGET_VERSION` (default
  `CURRENT_SYNC_SCHEMA_VERSION`) so a deploy can advance the wave
  deliberately rather than the moment the constant flips. Useful for
  staging: deploy with the constant=2 but the gate=1, watch the
  framework run as a no-op, then flip the env var to 2 to fire the
  wave.
- Watch the `prs_below_current_sync_schema_version` convergence
  canary: it spikes on deploy (every PR is now "below target=2") and
  trends back to 0 over days as the wave converges. If it stalls
  (flat or growing pass-over-pass), inspect the kick-budget and
  rate-limit telemetry.

### Chunk 6. `engagement_synced_at` deprecation (one release after Chunk 5)
- Stop writing `engagement_synced_at` from `PRSyncService`.
- Remove its filter clauses from `backfill_repo_engagement_task` (or
  remove the task entirely if `head_sha`/`head_ci_state` filling is
  subsumed by the v2 wave).
- Migration to drop the `engagement_synced_at` column.

## Validation Plan
- **Unit / integration tests:**
  - Existing (landed with Chunks 2 and 3b):
    - `qb_site/syncer/tests/services/test_sync_schema_upgrades.py` —
      register / stamp / dispatch contracts including the
      stale-pr-view race scenarios.
    - `qb_site/syncer/tests/tasks/test_upgrade_schema_tasks.py` — task
      plumbing, kick budget, settings defaults, active-fanout.
    - `qb_site/syncer/tests/tasks/test_collect_convergence_task.py`
      (extended) — `prs_below_current_sync_schema_version` and
      `sync_schema_version_target` populated correctly.
    - `qb_site/syncer/tests/services/test_inline_comments_sync.py` —
      thread-root resolution within and across reviews, idempotency,
      backfill marker, parse helper.
  - New (added with Chunk 4):
    - Extend `qb_site/syncer/tests/fixtures/` with
      `pr_bundle_with_engagement_events.json` covering the new
      `timelineItems` types and at least one review with multiple inline
      comments including a thread reply.
    - `qb_site/syncer/tests/services/test_engagement_event_ingestion.py`
      (or extension to `test_pull_request_sync.py`): each new event type
      maps to the correct row with correct
      `actor_login`/`occurred_at`/typed columns/`extra`; bot actors
      stored, not filtered; null/mannequin actors handled; pending
      reviews dropped; `REVIEW_DISMISSED` actor distinct from reviewer;
      team requested-reviewer routed to `requested_team_slug`,
      user/mannequin requested-reviewer routed to
      `requested_reviewer_login`; `inline_comment_total_count` populated
      from `comments.totalCount`.
    - End-to-end bundle ingest test (Chunk 4c): runs the fixture bundle
      through `sync_pull_request_bundle` and asserts the expected
      `PRTimelineEvent` + `PRReviewInlineComment` rows appear, with at
      least one fixture review having `pageInfo.hasNextPage=true` so
      the `PRReviewInlineCommentBackfill` row gets exercised end-to-end.
  - New (added with Chunk 5):
    - `qb_site/syncer/tests/services/test_v2_upgrader.py`: reset of
      `timeline_backfill_done`; `force=True` passthrough; idempotent
      under retries; `is_complete` returns True only after rewalk.
- **Manual checks:**
  - Pick a high-engagement mathlib4 PR (>50 reviews, >100 issue comments,
    multiple review threads with replies) and confirm post-upgrade event counts
    match GitHub's UI; spot-check inline comments on a known thread.
  - Confirm `sync_schema_version` advances 1 → 2 on a representative sample
    after an upgrader pass.
  - Watch token rate-limit budget during the upgrade wave; confirm
    `SYNCER_RATE_REMAINING_MIN` deferral behaves as on existing backfills.
  - Inspect bundle payload size before/after on a busy PR.
  - **Convergence dashboard sanity:** after each deploy, query the latest
    `SyncerConvergenceSnapshot` rows and confirm
    `sync_schema_version_target` matches `CURRENT_SYNC_SCHEMA_VERSION` in code
    and `prs_below_current_sync_schema_version` is monotonically decreasing
    pass-over-pass on each repo. A flat or growing line on this metric across
    multiple snapshots is the canary for a stalled wave.
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

## Deploy Boundaries
Each step is intended to be deployable on its own and roll back to the
previous deploy without manual intervention.

1. **Chunks 1+2 → deploy.** ✅ *Deployed 2026-05-08.* Schema columns + data
   migration. Upgrader framework + periodic task with an empty registry;
   v=1 is auto-stamped. Convergence canary went straight to 0.
2. **Chunks 3a + 3b → deploy.** Inline-comment models + ingestion service
   land together. Pure additive; service has no caller yet, so no behavior
   change.
3. **Chunk 4a → deploy.** GraphQL fragments + new
   `SYNCER_INLINE_COMMENTS_PER_REVIEW` setting. Bundle responses get
   bigger; ingestion still only recognizes the v1 event types so no new
   rows. Soak time for measuring payload growth on a busy PR via
   `gh api graphql`.
4. **Chunk 4b + 4c → deploy.** Normalizer recognizes the new event types
   and the inline-comments service is wired into the bundle. New
   `PRTimelineEvent` rows for issue comments / reviews / dismissals /
   review-requests start being created on fresh syncs; new
   `PRReviewInlineComment` and `PRReviewInlineCommentBackfill` rows
   appear. `CURRENT_SYNC_SCHEMA_VERSION` still 1, so no wave fires —
   only fresh syncs and ongoing timeline rewalks are affected. Spot-check
   that real-world data shape matches what Phase 0 verification told us
   to expect.
5. **Chunk 4d → deploy.** Strict `CHECK` constraints on `PRTimelineEvent`.
   Lands at least one deploy after 4b/4c so any unexpected GraphQL
   shapes have surfaced as test failures rather than as a production
   ingestion crash.
6. **Chunk 5 → deploy.** Bumps `CURRENT=2` and registers the v2 upgrader.
   The wave kicks off, paced by `SYNCER_SCHEMA_UPGRADE_KICK_LIMIT` plus
   the existing `SYNCER_RATE_REMAINING_MIN` deferral. The
   `prs_below_current_sync_schema_version` canary spikes on deploy and
   trends back to 0 over days as the wave converges.
7. **Chunk 6 → deploy** (post-soak). Drop `engagement_synced_at` and
   prune its filter clauses. Trivial cleanup.

Rollback: every step is additive (or, for Chunk 6, the column drop comes
after a release with no remaining writers/readers). For Chunks 4 and 5,
stopping the periodic beat task neutralizes the upgrader without further
intervention; redeploying the previous code keeps the migrations in place
harmlessly. For Chunk 4d, dropping the constraints in a hotfix migration
is a quick reversal if production data turns out to violate them.

## Open Questions (settle during implementation)

### Resolved
- ~~**`PRTimelineEvent` CHECK constraints — strict or loose?**~~ **Strict**
  (2026-05-08). The new typed columns (`requested_reviewer_login`,
  `requested_team_slug`, `inline_comment_total_count`) get CHECK
  constraints in Chunk 4d that mirror the existing
  `syncer_prtl_label_by_type_ck` / `syncer_prtl_sha_by_type_ck` pattern:
  the column may be set only when `type` is in the appropriate set.
  Constraints land *after* the ingestion code (4b/4c) has had at least
  one deploy of soak time, so any unexpected GraphQL shape surfaces as a
  test / staging failure rather than as a production ingestion crash.
  Phase 0 verification feeds into the exact constraint definitions.
- ~~**Bundle payload growth measurement.**~~ **`gh api graphql` against
  a busy PR**, no special tooling. Use the Phase 0 query template (in
  commit `24602eb` if it has been condensed out of this doc); record
  `rateLimit.cost` and response size before/after the Chunk 4a deploy
  to inform any tuning of `last:` / K.
- ~~**Phase 0 GraphQL field-shape findings.**~~ **Done 2026-05-08**;
  see Progress Notes. Headline: `requestedReviewer` union includes
  `Bot`, route to `requested_reviewer_login`. `ReviewDismissedEvent.review`
  is nullable, ingest needs a null guard (now in 4b).
  `state=DISMISSED` `PullRequestReview` nodes appear in `timelineItems`
  with non-null `submittedAt` and may carry inline comments —
  acknowledged in 4c (filter is `submittedAt is not null`, not a state
  allow-list).

### Open
- **Chunk 5 correctness pitfall: `is_complete(pr)` and v1-era
  `timeline_backfill_done=True`.** Recorded 2026-05-08 in §Chunk 5.
  Decide between the data-migration-reset approach and the
  `timeline_query_version` column approach. Recommended: data-migration
  reset (option (a)) unless the redundant-rewalk cost looks meaningful
  at our scale.
- Should the v2 upgrader live as its own Celery task or be folded into
  `backfill_repo_incomplete_prs`? Folding is simpler; separate gives
  cleaner observability per upgrade wave.
- Whether to keep writing `engagement_synced_at` across the deprecation
  window for rollback insurance, or stop writing it immediately and rely
  on `sync_schema_version >= 1`.
- `MERGED` event in v3: bundled with whatever the next expansion is, or
  its own version bump?

## Progress Notes
- 2026-05-07: Initial design drafted, then iteratively refined to:
  inline review comments captured via nested `PullRequestReview.comments`
  rather than punted to v3; `PRReviewInlineComment` model added (preserves
  the 1:1 timeline-node invariant); stamping rule moved entirely to the
  upgrader registry; reviewer-engagement query fields promoted to typed
  columns (`requested_reviewer_login`, `requested_team_slug`); the
  `inline_comments_incomplete` boolean replaced by GitHub-truth
  `inline_comment_total_count` + a dedicated `PRReviewInlineCommentBackfill`
  table; scan-performance subtleties added for all new periodic scans.
- 2026-05-07: Chunk 1 landed; Chunk 2's design refined — dispatcher
  auto-stamps versions with no registered upgrader (so v=1 needs no
  stamper-only upgrader); pacing split into
  `SYNCER_SCHEMA_UPGRADE_BATCH_SIZE` (DB-only stamping; default 1000) and
  `SYNCER_SCHEMA_UPGRADE_KICK_LIMIT` (GitHub-bound; default 20); two
  convergence-metric columns added to `SyncerConvergenceSnapshot`.
- 2026-05-08: Chunks 1+2 deployed to production. Convergence canary reports
  `prs_below_current_sync_schema_version=0` per active repo and
  `sync_schema_version_target=1`. Migration `0039`'s data step ran cleanly;
  no auto-stamp `INFO` log spam observed since the back-stock was already
  handled. Chunks 3a + 3b committed to the branch
  (`f7cf202`, `f9fd43d`); both purely additive, no behavior change in
  production until 4c wires the inline-comments service to the bundle
  ingest path. Resolved the constraint-strictness and payload-measurement
  open questions (see Open Questions); split Chunk 4 into Phase 0 + 4a–4d
  so each sub-chunk lands with bake time.
- 2026-05-08: **Phase 0 — API verification complete.** Combined live
  introspection (`__type` queries) with sample probes of three repos
  (`leanprover-community/mathlib4` PR 38292, `leanprover/lean4` PR 13628,
  `kubernetes/kubernetes` PR 129719). Findings, mapped to the Phase 0
  checklist:
  1. **`PullRequestReview.author`** — schema kind=`INTERFACE:Actor`
     (nullable). `Actor` interface includes `Bot`, `EnterpriseUserAccount`,
     `Mannequin`, `Organization`, `User`. Real-world sample: 30/30 reviews
     on mathlib4 PR 38292 had `User` authors; 5/5 approves on lean4 13628
     were `User`. No null `author` observed in the sample (small population
     of deleted accounts in our active mirrors), but the schema permits null.
     Design's "persist `author_login` as `\"\"` when null" convention is
     still required.
  2. **`PullRequestReview.submittedAt`** — `SCALAR:DateTime` (nullable).
     0 pending reviews observed in the sample; design's "drop pending
     reviews at ingest" remains correct since pending reviews surface
     with `submittedAt=null`.
  3. **`PullRequestReview.state`** values — schema enum exactly
     `{PENDING, COMMENTED, APPROVED, CHANGES_REQUESTED, DISMISSED}`,
     matching the design table. Real-world sample observed `COMMENTED`
     and `APPROVED` only (mathlib uses bors so `APPROVED` is rarer than
     in typical OSS; `COMMENTED` was dominant). `CHANGES_REQUESTED`
     and `DISMISSED` not observed in this sample but the enum guarantees
     they exist; `DISMISSED` we capture via the separate
     `ReviewDismissedEvent` rather than the review state.
  4. **`PullRequestReview.comments.totalCount`** — `NON_NULL(SCALAR:Int)`.
     Safe to rely on for `inline_comment_total_count`. Distribution on
     k8s PR 129719 (55 reviews): mostly `1` (single inline thread reply,
     the modern GitHub pattern), some `4`. **No review observed with
     `>20` inline comments** → `pageInfo.hasNextPage=False` for all
     reviews in the sample. `SYNCER_INLINE_COMMENTS_PER_REVIEW=20` is
     comfortable for current real-world data.
  5. **`ReviewDismissedEvent.previousReviewState`** —
     `NON_NULL(ENUM:PullRequestReviewState)`. Same enum as the review
     state itself. Storing in `extra.previous_review_state` works and
     never holds null.
  6. **`ReviewDismissedEvent.review`** — `OBJECT:PullRequestReview`
     (nullable). **Confirmed nullable**, so the design's null-guard at
     ingest is required. When `review` is null, `dismissed_review_*`
     fields in `extra` are stored as null (or omitted) instead of crashing.
  7. **`ReviewRequestedEvent.requestedReviewer`** — `UNION:RequestedReviewer`
     with possibleTypes `{Bot, Mannequin, Team, User}`. **Bot IS a member
     of the union** (contradicts a documentation footnote). Real-world
     samples showed only `User` requestees, but the schema permits all
     four. Routing decision: **Bot maps to `requested_reviewer_login`**
     (it has a `login`); `Team` → `requested_team_slug`; `User`/`Mannequin`
     → `requested_reviewer_login`. The 4d CHECK constraint should permit
     non-null `requested_reviewer_login` for Bot/Mannequin/User and
     non-null `requested_team_slug` for Team, with the two mutually
     exclusive.
  8. **`ReviewRequestedEvent.actor` / `ReviewRequestRemovedEvent.actor`**
     — `INTERFACE:Actor` (nullable). 0 null actors observed in the sample;
     design's "actor_login as empty string when null" applies.
  9. **`IssueComment.author`** — `INTERFACE:Actor` (nullable).
     **`IssueComment.createdAt`** — `NON_NULL(SCALAR:DateTime)`. Sampled
     authors include both `User` (117) and `Bot` (1) on k8s PR 129719;
     `Bot` (2) on mathlib4 PR 38292. Bot-authored comments are real
     and routinely present.

  **Decision points resolved:**
  - **CHECK constraints (4d)**: safe to write strictly per the original
    design. The only nuance is the `dismissed_review_*` extra fields,
    which are nullable in `extra` (no constraint needed since `extra`
    is JSON).
  - **`Bot` in `requestedReviewer`**: route to `requested_reviewer_login`.
  - **`last:250` page size**: bundle response on the busiest sample
    (k8s PR 129719 — 178 matched nodes, 55 reviews each with nested
    `comments(first:20)`) was on the order of tens of KB. Will measure
    real bundle size against current production after 4a deploy via
    a `gh api graphql` invocation against a mathlib4 active PR.
- 2026-05-08: **Chunk 4a implemented (uncommitted).**
  `qb_site/syncer/queries/{pr_bundle, timeline_page, timeline_page_back}.graphql`
  extended with the five new `itemTypes` and per-type fragments
  (`IssueComment`, `PullRequestReview` with nested
  `comments(first: $inlineCommentsPerReview)` plus `pageInfo.hasNextPage`
  and `totalCount`, `ReviewDismissedEvent`, `ReviewRequestedEvent`,
  `ReviewRequestRemovedEvent`). New GraphQL variable
  `$inlineCommentsPerReview: Int!` threaded through
  `GitHubClient.{get_pr_bundle,get_timeline_page,get_timeline_page_back}`
  with a default sourced from new setting
  `SYNCER_INLINE_COMMENTS_PER_REVIEW` (default `20`, in
  `qb_site/qb_site/settings/base.py` and `.env.example`).
  `scripts/validate_github_graphql.py` updated to pass the new variable;
  validation against the live GitHub schema passes for all three queries.
  `qb_site/syncer/AGENTS.md` `gh api graphql` example updated to include
  `-F inlineCommentsPerReview=20`.

  Behavior change: bundle/timeline responses now contain the additional
  event types and inline-comment connections. Normalizer in
  `timeline_sync.py` does **not** yet recognize these `__typename`s, so
  they fall through to the existing "unknown type" path (no DB writes,
  no errors). This is the intended Chunk 4a soak posture: payload
  growth measurable on real traffic, no ingestion change.

  Tests: `syncer.tests.client.test_github_client` extended to assert the
  new variable is captured; ran green locally (10/10). DB-requiring
  tests (integration / smoke / backfill) not run locally — Compose
  Postgres unavailable in this environment. Will need to run via
  `bash scripts/repo_check_compose.sh` before deploy.
- 2026-05-08: **Chunk 4b implemented.** Seven new values added to
  `PRTimelineEventType` (`ISSUE_COMMENTED`, `REVIEW_APPROVED`,
  `REVIEW_CHANGES_REQUESTED`, `REVIEW_COMMENTED`, `REVIEW_DISMISSED`,
  `REVIEW_REQUESTED`, `REVIEW_REQUEST_REMOVED`); metadata-only migration
  `0042_alter_prtimelineevent_type.py` regenerates the `choices=` set
  on the column. CHECK constraints are deferred to 4d.

  `qb_site/syncer/services/sub/timeline_sync.py` was refactored to
  extract per-event field translation into `_extract_event_fields(ev)`
  and now handles all five new GraphQL types from Chunk 4a:

  - **`IssueComment`** → `ISSUE_COMMENTED`. `actor_login` from
    `author.login` with the empty-string convention for null/deleted
    accounts. `createdAt` populates `occurred_at`.
  - **`PullRequestReview`** → `REVIEW_*` per `state` (`APPROVED` /
    `CHANGES_REQUESTED` / `COMMENTED`). `PENDING` and `DISMISSED`
    review states are dropped at ingest (the latter is captured via
    the separate `ReviewDismissedEvent`). `submittedAt` populates
    `occurred_at`; `inline_comment_total_count` is read from
    `comments.totalCount`. The update path refreshes
    `inline_comment_total_count` on rewalks (it's GitHub-truth and may
    grow), but other typed columns remain append-only.
  - **`ReviewDismissedEvent`** → `REVIEW_DISMISSED`. `actor_login` is
    derived from `actor`, never from `review.author` (per design's
    invariant). `extra` carries `dismissed_review_node_id` /
    `dismissed_review_author` / `dismissed_review_submitted_at` /
    `previous_review_state`. **Null-guard for `review`**: when
    GitHub returns `review: null`, only `previous_review_state` is
    populated in `extra`.
  - **`ReviewRequestedEvent`** / **`ReviewRequestRemovedEvent`** →
    `REVIEW_REQUESTED` / `REVIEW_REQUEST_REMOVED`. `requestedReviewer`
    routing per Phase 0: `User` / `Bot` / `Mannequin` →
    `requested_reviewer_login`; `Team` → `requested_team_slug`. The
    two columns are mutually exclusive (only one is populated per row).

  New tests in `syncer.tests.subsystems.test_timeline_sync.TestTimelineSyncReviewAndCommentEvents`
  (16 cases) cover: each new event type's mapping; pending review
  dropped; `state=DISMISSED` on `PullRequestReview` dropped (captured
  via `ReviewDismissedEvent` instead); `inline_comment_total_count`
  refresh on rewalk; dismissed-review denormalization; dismissed
  null-`review` guard; User/Team/Bot/Mannequin reviewer routing for
  `REVIEW_REQUESTED`; `REVIEW_REQUEST_REMOVED` reuses the same routing;
  null-actor stored as empty string; bot author on `IssueComment`.
  Pure-function smoke (`_extract_event_fields`) ran green on all paths.

  Behavior at deploy: with 4a's queries already returning the new
  `__typename`s, this chunk lights up ingestion. New rows on fresh
  syncs and on timeline backfill rewalks. `CURRENT_SYNC_SCHEMA_VERSION`
  is still `1`, so the v=2 wave does not fire; only PRs being synced
  for unrelated reasons (or appearing for the first time) get the new
  rows. Inline-comment ingestion is **not yet wired** — that's Chunk
  4c. So `PRReviewInlineComment` rows do not appear yet, but the
  parent `REVIEW_*` rows do, and `inline_comment_total_count` carries
  the GitHub-truth count for analytics.

  DB-requiring tests not run locally (no Compose). Need
  `bash scripts/repo_check_compose.sh` before deploy.
- 2026-05-08: **Pre-4c verification.** Before wiring 4c, ran additional
  `gh api graphql` probes to confirm two assumptions the design depends on:

  1. **`state=DISMISSED` `PullRequestReview` nodes appear in
     `timelineItems` with non-null `submittedAt` and may carry inline
     comments.** Verified on `rust-lang/rust` PR 149543, which has 8
     reviews in its sample (states: 5 COMMENTED, 1 CHANGES_REQUESTED,
     1 APPROVED, 1 DISMISSED). The dismissed review's `submittedAt` is
     real (`2026-01-14T18:26:51Z`) and its `comments.totalCount=1`
     with one persistent inline comment. **Implication:** dropping
     `state=DISMISSED` `PullRequestReview` nodes at the
     `PRTimelineEvent` level (per 4b) is correct — the dismissal is
     captured via the separate `ReviewDismissedEvent` — but the inline
     comments under that review still carry real review feedback and
     would be lost if 4c skipped them. So 4c's filter is
     **`submittedAt is not null`** rather than a state allow-list:
     DISMISSED reviews' inline comments are persisted with
     `parent_review_event=NULL`, durably linked via `review_node_id`.
     Backfill markers are skipped for these (no parent FK to anchor
     the OneToOne row); accept the gap, log a warning. PENDING
     reviews are dropped naturally because `submittedAt is null`.
  2. **Thread-reply pattern: each thread reply is wrapped in its own
     one-comment `PullRequestReview` with `state=COMMENTED`.**
     Verified on `rust-lang/rust` PR 153826's `reviewThreads`. A
     three-comment thread had three distinct `PullRequestReview` ids;
     the second and third comments' `replyTo.id` correctly pointed
     back to the first comment, not to each other. Confirms the
     design's invariant and the inline-comments service's
     bundle-scope thread-root resolution.
- 2026-05-08: **Chunk 4c implemented.** `PRSyncService.sync_pull_request_bundle`
  now invokes `_sync_inline_review_comments(pr_obj, tl_nodes)` after
  the timeline-sync call. The helper:

  - Filters timeline nodes to `PullRequestReview` items with non-null
    `submittedAt` (drops PENDING).
  - Resolves the `parent_review_event` FK by looking up
    `PRTimelineEvent` rows by `github_node_id`. For DISMISSED reviews,
    no parent row exists, so the FK is left null — see Pre-4c
    verification.
  - Builds one `ReviewInlineCommentsGroup` per review via the existing
    `parse_review_inline_comments_group` helper, then calls
    `sync_review_inline_comments_bundle` once per bundle. Bundle scope
    is required so the service can resolve thread roots across
    reviews (modern GitHub wraps each thread reply in its own
    one-comment review).

  Result dict gains `inline_comments_created` and
  `inline_backfill_rows_upserted` keys so the sync_pr task's summary
  surfaces ingestion volumes for monitoring.

  New fixture `qb_site/syncer/tests/fixtures/pr_bundle_with_engagement_events.json`
  exercises every shape: a team review request, a Bot review request,
  an issue comment, two reviews under the same thread (root + reply,
  validating cross-review thread root resolution), an APPROVED review
  with `comments.pageInfo.hasNextPage=true` to trigger the backfill
  marker, a PENDING review (must be dropped), and a DISMISSED review
  with one inline comment + the matching `ReviewDismissedEvent` (must
  ingest the inline comment with `parent_review_event=NULL`).

  New tests in
  `qb_site/syncer/tests/services/test_engagement_event_ingestion.py`
  (9 cases): row counts, team/bot reviewer routing, issue-comment
  shape, inline-comment persistence with thread-root self-anchoring,
  cross-review thread reply linking, backfill row created with
  `total_count=25`, pending-review drop, dismissed-review inline
  comments captured with null parent FK, idempotency under re-ingest,
  result-dict counts.

  Behavior at deploy: with 4a's fragments already returning the new
  shapes and 4b's normalizer creating REVIEW_* rows, this chunk lights
  up `PRReviewInlineComment` and `PRReviewInlineCommentBackfill`
  ingestion. New rows on fresh syncs and timeline rewalks.
  `CURRENT_SYNC_SCHEMA_VERSION` is still `1`, so no v=2 wave fires —
  only PRs being synced for unrelated reasons get the new rows.

  DB-requiring tests not run locally (no Compose). Need
  `bash scripts/repo_check_compose.sh` before deploy.
- 2026-05-08: **Pre-deploy discussion of fresh-sync vs Chunk 5
  interaction.** Question raised: do fresh syncs after the 4a–4c deploy
  bump `sync_schema_version` so PRs aren't re-enqueued by Chunk 5?
  Answer: no, PRSyncService doesn't write `sync_schema_version` (the
  upgrader registry is the sole writer, by Subtleties invariant). So
  every PR remains at v=1 until Chunk 5 fires.

  This surfaced a real correctness gap in the original Chunk 5 plan:
  the rule `is_complete(pr) := pr.timeline_backfill_done` causes PRs
  with v1-era `timeline_backfill_done=True` to be stamped to v=2
  *without* a rewalk under v2's broader `itemTypes` — historical
  `IssueComment` / `PullRequestReview` events would be silently
  skipped. Two fixes are now documented in §Chunk 5: (a) data-migrate
  `timeline_backfill_done=False` for all `sync_schema_version<2` PRs
  at Chunk 5 deploy, or (b) add a `timeline_query_version` column on
  `PullRequest` that PRSyncService writes when a full-history walk
  completes. Recommendation: option (a). Tracking as an open question.

## Finalization Notes
- After v2 ships and `engagement_synced_at` is dropped, convert this doc into
  a concise final-decision record describing: the upgrader framework, the set
  of event types captured at v2, `PRReviewInlineComment`, and the deprecated
  mechanism. Move chunked rollout details to git history.
