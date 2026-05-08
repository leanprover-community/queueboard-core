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
| 2. Upgrader framework + convergence metric | **Deployed** | Periodic task `syncer.upgrade_schema_versions` is firing; convergence canary reports `prs_below_current_sync_schema_version=0` and `sync_schema_version_target=1` per repo. Registry empty as designed; auto-stamp path is what's running. |
| 3a. Inline-comment models + admin + backup policy | **Committed, awaiting deploy** | Pure additive migration `0041`; both new tables and admins ready; `validate_backup_policy.py` updated. |
| 3b. Inline-comment ingestion service | **Committed, awaiting deploy** | `qb_site/syncer/services/sub/inline_comments_sync.py` + tests. Service is importable but unreferenced; Chunk 4 wires it in. |
| 4 — Phase 0. API verification | **Done** | Schema introspection + sample queries against mathlib4, lean4, kubernetes/kubernetes confirm field shapes. See Progress Notes 2026-05-08 (Phase 0). |
| 4a. GraphQL fragments + setting | **In progress (uncommitted)** | Fragments added to `pr_bundle.graphql`, `timeline_page.graphql`, `timeline_page_back.graphql`; `$inlineCommentsPerReview` threaded; `SYNCER_INLINE_COMMENTS_PER_REVIEW` (default 20) added; `validate_github_graphql.py` updated. |
| 4b. Normalizer + REVIEW_*/ISSUE_COMMENTED ingestion | Pending | Map new `__typename`s in `timeline_sync.py`; new `PRTimelineEventType` values + migration. |
| 4c. Wire inline-comments service | Pending | Connect existing `inline_comments_sync.py` to bundle ingest path. |
| 4d. Strict CHECK constraints | Pending | After 4b/4c soak. |
| 5. v2 upgrader | Pending | Bumps `CURRENT_SYNC_SCHEMA_VERSION = 2`; the wave fires here. |
| 6. `engagement_synced_at` deprecation | Pending | Post-soak after Chunk 5. |

The branch carries Chunks 3a + 3b uncommitted-relative-to-main. Chunks 1+2
are already in production and behaving as designed.

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
Chunks 1, 2, 3a, 3b are complete; details preserved in git history. The
sections below describe what remains.

### Chunk 4 — GraphQL + new timeline event types + wiring
Split into a Phase 0 verification step (no code) followed by four small
sub-chunks. Each sub-chunk is intended to land cleanly on its own; the
deploy boundary the Deploy Boundaries section lays out (3+4 together) means
in practice 3a → 3b → 4a → 4b → 4c → 4d arrive in some sequence with bake
time between, before Chunk 5 fires the wave.

#### Phase 0. Verify GitHub API behavior (no code)
**Why first.** Chunk 4 introduces `CHECK` constraints that reject ingestion
if the persisted shape doesn't match expectations. We want the constraints
to be strict (per Open Question (a) resolution), but only after we've
confirmed the GraphQL responses behave as documented — otherwise a strict
constraint can cause a sync to crash on edge-case data we didn't anticipate.

**What to confirm.** Run `gh api graphql` queries against
`leanprover-community/mathlib4` (and any other busy active repo) and record
the answers in this doc's Progress Notes:

1. **`PullRequestReview.author` nullability.** Confirm whether `author`
   can come back null (deleted account) and whether `Bot` is a valid
   member of the underlying `Actor` interface. Sample query: pick a PR
   with a known bot reviewer and a PR that's old enough that some
   reviewer accounts may have been deleted.
2. **`PullRequestReview.submittedAt`** — confirm pending reviews
   (drafts) come back with `submittedAt: null`. The design's invariant
   "drop pending reviews at ingest" depends on this.
3. **`PullRequestReview.state` values.** Confirm the only values we see
   are `APPROVED`, `CHANGES_REQUESTED`, `COMMENTED`, `DISMISSED`, and
   `PENDING`. (The doc table maps the first three to typed columns and
   skips `PENDING`; `DISMISSED` is captured via the separate
   `ReviewDismissedEvent`, not the review state.)
4. **`PullRequestReview.comments.totalCount`** — confirm it's
   non-nullable so we can rely on it for the `inline_comment_total_count`
   typed column.
5. **`ReviewDismissedEvent.previousReviewState`** — possible values
   and nullability. The design table puts this in `extra`, so a null is
   storable, but documenting the observed values informs the constraint
   on the parent `type` column.
6. **`ReviewDismissedEvent.review`** — could the dismissed review be
   null (e.g., if the review was hard-deleted)? The design relies on
   reading `review.id` / `review.author` / `review.submittedAt` for the
   `extra` denormalization; if `review` can be null, we need a null
   guard at ingest.
7. **`ReviewRequestedEvent.requestedReviewer`** — the union members
   we actually see. The design names `User`, `Team`, `Mannequin`; we
   need to confirm whether `Bot` (or any other `Actor`) ever shows up
   as a `requestedReviewer`. If `Bot` does, decide whether to route it
   to `requested_reviewer_login` (it has a `login`) or extend the
   union handling.
8. **`ReviewRequestedEvent.actor` / `ReviewRequestRemovedEvent.actor`**
   — null possibility (deleted requester). The design's "actor_login as
   empty string when null" convention covers this; sample to confirm.
9. **`IssueComment.author` and `IssueComment.createdAt`** — both
   should be present (createdAt is required by GraphQL; author may be
   null for deleted accounts).

**Method.**
```bash
gh api graphql -F query='
  query($owner: String!, $name: String!, $number: Int!) {
    repository(owner: $owner, name: $name) {
      pullRequest(number: $number) {
        timelineItems(last: 250, itemTypes: [
          ISSUE_COMMENT, PULL_REQUEST_REVIEW,
          REVIEW_DISMISSED_EVENT, REVIEW_REQUESTED_EVENT,
          REVIEW_REQUEST_REMOVED_EVENT,
          LABELED_EVENT, UNLABELED_EVENT, ASSIGNED_EVENT, UNASSIGNED_EVENT,
          READY_FOR_REVIEW_EVENT, CONVERT_TO_DRAFT_EVENT,
          REOPENED_EVENT, CLOSED_EVENT, HEAD_REF_FORCE_PUSHED_EVENT
        ]) {
          totalCount
          pageInfo { hasPreviousPage startCursor }
          nodes {
            __typename
            ... on IssueComment { id createdAt author { __typename login } }
            ... on PullRequestReview {
              id submittedAt state
              author { __typename login }
              comments(first: 20) {
                pageInfo { hasNextPage }
                totalCount
                nodes {
                  id createdAt path line originalLine
                  replyTo { id }
                  author { __typename login }
                }
              }
            }
            ... on ReviewDismissedEvent {
              id createdAt previousReviewState
              actor { __typename login }
              review { id submittedAt author { __typename login } }
            }
            ... on ReviewRequestedEvent {
              id createdAt
              actor { __typename login }
              requestedReviewer {
                __typename
                ... on User { login }
                ... on Team { slug }
                ... on Mannequin { login }
                ... on Bot { login }
              }
            }
            ... on ReviewRequestRemovedEvent {
              id createdAt
              actor { __typename login }
              requestedReviewer {
                __typename
                ... on User { login }
                ... on Team { slug }
                ... on Mannequin { login }
                ... on Bot { login }
              }
            }
          }
        }
      }
      rateLimit { cost remaining }
    }
  }
' -F owner=leanprover-community -F name=mathlib4 -F number=<PR_NUMBER>
```

Pick three to five PRs spanning: a busy currently-open PR (>50 reviews,
>100 issue comments), an older closed PR with a deleted-account author, a
PR that has had a dismissed review, a PR that has a team review request,
and a PR with a Bot as a reviewer (e.g., a Dependabot PR).

**Outputs.** Append observations to the Progress Notes — particularly
any unexpected null fields, additional union members on
`requestedReviewer`, and the `comments.totalCount` distribution (this
informs whether `SYNCER_INLINE_COMMENTS_PER_REVIEW=20` is right). Use
the `rateLimit.cost` from the response plus the response byte size to
compare against today's `last: 250` cost.

**Decision points.** From the findings, decide:
- Whether the planned `CHECK` constraints in 4d are safe to write
  strictly, or whether any need a permissive form.
- Whether `Bot` as `requestedReviewer` should be routed to
  `requested_reviewer_login` (treat as a login-bearing actor) or stored
  in `extra` only.
- Whether `last: 250` should drop given the new fragment cost.

#### Chunk 4a. GraphQL fragments + per-review-comments setting
Bundle responses get larger but no new ingestion happens — the normalizer
still doesn't recognize the new types, so they're filtered out. Lets us
measure payload growth in production with minimal risk.

- Extend `qb_site/syncer/queries/pr_bundle.graphql`,
  `qb_site/syncer/queries/timeline_page.graphql`,
  `qb_site/syncer/queries/timeline_page_back.graphql`:
  - Add to `itemTypes`: `ISSUE_COMMENT`, `PULL_REQUEST_REVIEW`,
    `REVIEW_DISMISSED_EVENT`, `REVIEW_REQUESTED_EVENT`,
    `REVIEW_REQUEST_REMOVED_EVENT`.
  - Add per-type fragments per the table in §3 of this doc.
  - On `PullRequestReview`, add nested
    `comments(first: $inlineCommentsPerReview)` with `id, createdAt,
    path, line, originalLine, replyTo { id }, author { login }` plus
    `pageInfo { hasNextPage }` and `totalCount`.
  - Add a new GraphQL variable `$inlineCommentsPerReview: Int!` to all
    three queries.
- Add setting `SYNCER_INLINE_COMMENTS_PER_REVIEW` (default `20`) in
  `qb_site/qb_site/settings/base.py` and `.env.example`. Wire the value
  into the GraphQL client calls in `qb_site/syncer/services/github_client.py`.
- After deploy, watch for: bundle payload size (compare before/after on
  a busy PR via the manual `gh api graphql` invocation), rate-limit
  budget over a few hours of normal sync traffic, any timeouts.
- No behavior change beyond payload size. Normalizer rejects the new
  `__typename` values silently (they fall through the type-map check,
  same as today's unknown types).

#### Chunk 4b. Normalizer + enum + REVIEW_* / ISSUE_COMMENTED ingestion
New `PRTimelineEvent` rows start being created on fresh syncs (and on
the timeline backfill rewalks). No new tables touched yet.

- Add the seven new values to `PRTimelineEventType` in
  `qb_site/syncer/models/pr_timeline_event.py`: `ISSUE_COMMENTED`,
  `REVIEW_APPROVED`, `REVIEW_CHANGES_REQUESTED`, `REVIEW_COMMENTED`,
  `REVIEW_DISMISSED`, `REVIEW_REQUESTED`, `REVIEW_REQUEST_REMOVED`.
- Migration that adds the enum values (Django's `TextChoices` change
  is metadata-only on the model; no DB-level changes unless you add
  CHECK constraints — those are deferred to 4d).
- Extend `qb_site/syncer/services/sub/timeline_sync.py`:
  - Map the new `__typename` values to `PRTimelineEventType`.
  - For each, extract `actor_login` / `occurred_at` per the design
    table.
  - For `PullRequestReview`: map `state` to the right `REVIEW_*`
    type; drop pending reviews (`submittedAt is None`); populate
    `inline_comment_total_count` from `comments.totalCount`.
  - For `REVIEW_REQUESTED` / `REVIEW_REQUEST_REMOVED`: route
    `requestedReviewer` by `__typename` to either
    `requested_reviewer_login` (User / Mannequin / Bot if Phase 0
    confirms) or `requested_team_slug` (Team).
  - For `REVIEW_DISMISSED`: populate `extra.dismissed_review_node_id`,
    `extra.dismissed_review_author`, `extra.dismissed_review_submitted_at`,
    `extra.previous_review_state`. Always derive `actor_login` from
    `actor`, never from `review.author`.
- Honor the convention: persist `actor_login` as `""` when GraphQL
  returns null; same for `requested_reviewer_login` (use empty string
  rather than null when the underlying object is present but the login
  is null — should not happen in practice).
- Add tests under `qb_site/syncer/tests/services/` (or expand
  `test_pull_request_sync.py`): each new event type maps to the right
  row; pending review dropped; null/Bot/Mannequin actors routed
  correctly; dismissed-review denormalization populated.

#### Chunk 4c. Wire inline-comments service + reuse the bundle scope
Connects the existing (unreferenced) `inline_comments_sync` service to
the bundle ingest path. New rows in `PRReviewInlineComment` and
`PRReviewInlineCommentBackfill` start appearing on fresh syncs.

- In the bundle-level orchestrator (`PRSyncService.sync_pull_request_bundle`),
  after the `timeline_sync` call has persisted any `REVIEW_*` events,
  collect tuples of
  `(review_node_id, parent_review_event, comments_connection)` for
  each review node in the bundle and call
  `sync_review_inline_comments_bundle(...)` once per bundle.
- Bundle scope is required so the service can resolve thread roots
  across reviews (modern GitHub wraps each thread reply in its own
  review).
- Add an integration test under `qb_site/syncer/tests/` that runs a
  fixture bundle (`pr_bundle_with_engagement_events.json`) end-to-end
  through `sync_pull_request_bundle` and asserts the expected
  `PRTimelineEvent` + `PRReviewInlineComment` rows appear, with one
  fixture review having `pageInfo.hasNextPage=true` so the
  `PRReviewInlineCommentBackfill` row gets exercised.

#### Chunk 4d. Strict CHECK constraints
Final sub-chunk of Chunk 4. Should land at least one deploy after 4c so
real-world data has flowed through the new ingestion path and any
edge-case shapes the Phase 0 verification missed have surfaced.

- Migration adds CHECK constraints on `PRTimelineEvent` mirroring the
  existing `syncer_prtl_label_by_type_ck` / `syncer_prtl_sha_by_type_ck`
  pattern:
  - `requested_reviewer_login` + `requested_team_slug` set only when
    `type IN (REVIEW_REQUESTED, REVIEW_REQUEST_REMOVED)`.
  - `requested_reviewer_login` and `requested_team_slug` mutually
    exclusive (at most one is non-null on a given row).
  - `inline_comment_total_count` set only when
    `type IN (REVIEW_APPROVED, REVIEW_CHANGES_REQUESTED, REVIEW_COMMENTED)`.
- Constraints land *after* 4c has been deployed so any shape mismatch
  surfaces as test/staging failures rather than as a production
  ingestion crash. If Phase 0 surfaced an edge case (e.g. Bot
  reviewers), encode the chosen routing here so the constraint
  reflects production reality.

### Chunk 5. v2 upgrader
- Register `upgrade_to_v2` (`is_complete`, `kick`) at version 2 in
  `qb_site/syncer/services/sync_schema_upgrades.py`. `is_complete(pr)`
  returns `pr.timeline_backfill_done`. `kick(pr)`:
  1. If `timeline_backfill_done=True`, set it to `False` and clear
     `timeline_backfill_cursor` so the existing
     `backfill_repo_incomplete_prs` task re-walks history with the new
     `itemTypes` and the nested `comments(first: K)` fetch.
  2. Enqueue `sync_pr_task(force=True)` so dedupe doesn't swallow the
     rewalk.
- Bump `CURRENT_SYNC_SCHEMA_VERSION = 2`.
- Add settings gate `SYNCER_SCHEMA_UPGRADE_TARGET_VERSION` (default
  `CURRENT_SYNC_SCHEMA_VERSION`) so a deploy can advance the wave
  deliberately rather than the moment the constant flips.
- Watch the `prs_below_current_sync_schema_version` convergence canary;
  it should rise sharply on deploy as everything goes from "below
  target=2" and then trend back down as the wave progresses.

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
  a busy PR**, no special tooling. The Phase 0 query in §Chunk 4 is the
  template; record `rateLimit.cost` and response size before/after the
  Chunk 4a deploy to inform any tuning of `last:` / K.

### Open
- **Phase 0 GraphQL field-shape findings.** Chunk 4 should *not* begin
  the constraint-design step (4d) before Phase 0's verification queries
  are run and their answers recorded in Progress Notes. The questions
  to resolve are listed under "Phase 0. Verify GitHub API behavior" in
  §Chunk 4.
- Should the v2 upgrader live as its own Celery task or be folded into
  `backfill_repo_incomplete_prs`? Folding is simpler; separate gives
  cleaner observability per upgrade wave.
- Whether to keep writing `engagement_synced_at` across the deprecation
  window for rollback insurance, or stop writing it immediately and rely
  on `sync_schema_version >= 1`.
- `MERGED` event in v3: bundled with whatever the next expansion is, or
  its own version bump?

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
- 2026-05-07: Chunk 1 landed (schema + stub upgrader module). Refined Chunk
  2's design ahead of implementation: dispatcher auto-stamps versions with
  no registered upgrader (so v=1 doesn't need a trivial stamper-only
  upgrader, since `PRSyncService` already writes the engagement fields v=1
  represents on every sync). Split the pacing knobs into
  `SYNCER_SCHEMA_UPGRADE_BATCH_SIZE` (DB-only stamping; default 1000) and
  `SYNCER_SCHEMA_UPGRADE_KICK_LIMIT` (GitHub-bound; default 20) so Chunk 5's
  v=2 wave is paced like the engagement backfill while stamping clears
  cheap backlogs in one or two passes. Added two convergence-metric columns
  to `SyncerConvergenceSnapshot` so wave progress is observable per repo.
  Documented deploy boundaries (1+2, 3+4, 5, 6) so each impact step has a
  soak period before the next.
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

## Finalization Notes
- After v2 ships and `engagement_synced_at` is dropped, convert this doc into
  a concise final-decision record describing: the upgrader framework, the set
  of event types captured at v2, `PRReviewInlineComment`, and the deprecated
  mechanism. Move chunked rollout details to git history.
