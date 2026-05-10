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
| 3a. Inline-comment models + admin + backup policy | **Deployed** | Pure additive migration `0041`; both new tables and admins; `validate_backup_policy.py` updated. |
| 3b. Inline-comment ingestion service | **Deployed** | `qb_site/syncer/services/sub/inline_comments_sync.py` + tests. |
| 4a. GraphQL fragments + setting | **Deployed** | Fragments added to `pr_bundle.graphql`, `timeline_page.graphql`, `timeline_page_back.graphql`; `$inlineCommentsPerReview` threaded; `SYNCER_INLINE_COMMENTS_PER_REVIEW` (default 20) added. |
| 4b. Normalizer + REVIEW_*/ISSUE_COMMENTED ingestion | **Deployed** | 7 new `PRTimelineEventType` values + metadata-only migration `0042`; `timeline_sync.py` extended to map new `__typename`s with state-routing, pending-review drop, dismissed-review null-guard, requestedReviewer routing, and `inline_comment_total_count` refresh. |
| 4c. Wire inline-comments service (bundle path only) | **Deployed (2026-05-07)** with gap | `PRSyncService.sync_pull_request_bundle` calls `sync_review_inline_comments_bundle`. **Gap:** the same call was NOT wired into `sync_pull_request`'s forward `get_timeline_page` and backward `get_timeline_page_back` loops. Discovered post-Chunk-5 deploy when v=2 PRs began appearing without `PRReviewInlineComment` rows. Fixed in **Chunk 5b**. |
| 4d. Strict CHECK constraints | **Deployed (2026-05-08)** | Migration `0043` added three constraints: `syncer_prtl_requested_reviewer_by_type_ck`, `syncer_prtl_requested_reviewer_mutex_ck`, `syncer_prtl_inline_total_by_type_ck`. Pre-flight queries against prod returned 0/0/0 before deploy. |
| 5. v2 upgrader (the wave) | **Deployed (2026-05-08)** with gap | `CURRENT_SYNC_SCHEMA_VERSION=2`; `UpgradeToV2` registered via `SyncerConfig.ready()`. Migration `0044` reset `timeline_backfill_done=False, timeline_backfill_cursor=NULL` for every PR at v<2. **Gap:** because of the 4c gap, v=2 PRs were stamped without their inline comments — the historical pages didn't invoke the inline-comments service. Mitigated by Chunk 5b's v=3 wave; recommend setting `SYNCER_SCHEMA_UPGRADE_TARGET_VERSION=1` between detection and the 5b deploy to stop growing the affected cohort. |
| 5b. Page-path inline-comments fix + v3 wave | **Deployed (2026-05-08)** | Fixed `_sync_inline_review_comments` invocation in both `sync_pull_request` page loops. `CURRENT_SYNC_SCHEMA_VERSION` bumped to 3; `UpgradeToV3` (mechanically identical to v2) registered. Migration `0045` reset `timeline_backfill_done=False` for every PR at v<3 (same option (a) approach as 0044). New regression tests in `tests/services/test_inline_comments_page_paths.py`. See §Chunk 5b. Soak observation through 2026-05-09: v=3 wave converging smoothly, `prs_below_current_sync_schema_version` trending down monotonically per repo, `PRReviewInlineComment` row count growing in lockstep, `PRReviewInlineCommentBackfill` markers now appearing for long-tail reviews. |
| 6a. Stop writing + retire engagement-backfill task | **Deployed (2026-05-09)** | Commit `e51cecc`. Soaked cleanly through 2026-05-10 — no regressions in `_data_status`, beat schedule, or convergence snapshots; `engagement_synced_at` values frozen on existing PRs as designed. |
| 6b. Drop columns + snapshot metrics | **Implemented (2026-05-10)** | Migration `0047_drop_engagement_synced_at_and_snapshot_metrics` issues three `RemoveField` ops (`pullrequest.engagement_synced_at`, `syncerconvergencesnapshot.prs_missing_engagement`, `syncerconvergencesnapshot.prs_engagement_incomplete`). Model fields and admin entries removed in lockstep; `docs/queueboard_api_contract.md` and `docs/design-decisions/017-token-cost-tracking.md` updated. `scripts/backup_policy.py` requires no edit (no field-level allowlist; all retained tables export with `SELECT *`). |

### Resumption pointer for the next agent
Chunks 1–4, 5, and **5b are now in production** as of 2026-05-08; the
v=3 recovery wave has been converging smoothly through 2026-05-09 with
`prs_below_current_sync_schema_version` trending down monotonically per
repo and `PRReviewInlineComment` row counts growing in lockstep.

The remaining work is **Chunk 6** — removing `engagement_synced_at`
and the `backfill_repo_engagement[_active]` task that scanned for it.
**No schema-version bump is needed** (see §Chunk 6 "Why no schema-
version bump"); this is a code/schema cleanup, not an ingestion
expansion. The plan is split across two deploys (6a stop writing +
remove the task + remove the engagement metric; 6b drop the column
and the two snapshot fields), each independently rollback-safe.

Historical context (kept here so a future agent reading after Chunk 6
ships still sees what shipped):

1. **Pre-deploy: gate the v=2 wave to a halt.** Set
   `SYNCER_SCHEMA_UPGRADE_TARGET_VERSION=1` in production before the
   5b code lands. This stops growing the "v=2 with missing inline
   comments" cohort. Without the gate, every PR processed during the
   gap window gets walked twice (once under broken code → v=2, once
   under fixed code → v=3); with the gate, paused PRs go straight
   from v=1 → v=3 in one rewalk. The convergence canary will stay
   flat-and-large during the gate — that's expected.
2. **Chunk 5b — deploy.** Three migrations land together:
   - `0045_reset_timeline_backfill_for_v3_wave` — resets
     `timeline_backfill_done=False` for every PR at v<3 (option (a)
     applied a second time; same pattern as 0044).
   - `0046_dismissed_review_synthesis_and_inline_schema_tightening` —
     does a primary-key swap on `PRReviewInlineCommentBackfill`
     (`review_event` → auto `id` PK; `review_event` becomes nullable;
     `review_node_id` becomes the unique key). The PK swap uses
     `SeparateDatabaseAndState` with raw SQL because Django's
     auto-generated ordering would emit two PRIMARY KEYs at once,
     which Postgres rejects. Then adds a `review_node_id != ''`
     CHECK on `PRReviewInlineComment`, an index on `reply_to_node_id`,
     and runs two bulk-SQL data backfills: synthesize
     `REVIEW_<previousReviewState>` rows for existing
     `REVIEW_DISMISSED` events (Item 1) and link inline comments
     whose parent is now present.
   The bumped constant + registered `UpgradeToV3` (mechanically
   identical to v2) then drive rewalks via the existing
   `backfill_repo_incomplete_prs` pacing — those rewalks correctly
   persist the inline comments because the page loops in
   `pr_sync_service.py` invoke `_sync_inline_review_comments`.
3. **Post-deploy: lift the gate.** Unset
   `SYNCER_SCHEMA_UPGRADE_TARGET_VERSION` (or set to `3`) so the
   wave fires.

   **Watch list during the wave:**
   - `prs_below_current_sync_schema_version` per repo — must be
     monotonically decreasing pass-over-pass.
   - `PRReviewInlineComment` row count per repo — should now be
     growing in lockstep with the cohort transitioning v=2 → v=3.
   - `PRReviewInlineCommentBackfill` row count — long-tail reviews
     with >`SYNCER_INLINE_COMMENTS_PER_REVIEW` inline comments
     should now produce rows here. Since the table is now keyed on
     `review_node_id` with a nullable `review_event`, dismissed
     reviews with `review: null` on GitHub also produce rows
     (previously skipped).
   - `PRTimelineEvent` row count per repo — will jump on first deploy
     by the count of `REVIEW_DISMISSED` rows whose `extra` carried a
     `dismissed_review_node_id` (one synthesized parent per dismiss
     event). This is migration 0046's data backfill landing; no
     action needed.
   - GitHub rate-limit telemetry — same posture as the v=2 wave.

   **Post-deploy SQL spot-checks (run a few hours after deploy):**
   - The "v=2 PRs with missing inline comments" cohort query in
     §Validation Plan should return 0 once the wave has covered them
     (rises to v=3 with parent-FK linked inline comments). Until the
     wave converges this stays positive but should trend down.
   - `SELECT COUNT(*) FROM syncer_prtimelineevent ev WHERE ev.type IN
     ('REVIEW_APPROVED', 'REVIEW_CHANGES_REQUESTED',
     'REVIEW_COMMENTED') AND EXISTS (SELECT 1 FROM
     syncer_prtimelineevent dis WHERE dis.type = 'REVIEW_DISMISSED'
     AND dis.extra->>'dismissed_review_node_id' = ev.github_node_id)`
     — count of synthesized parents (each pairs with a dismiss
     event). Should match the count of dismiss events that had a
     non-null `review` field.
4. **Chunk 6 — drop `engagement_synced_at`.** Trivial cleanup, after
   5b has soaked at least one release.

If the wave stalls (the canary stays flat across multiple snapshots
on a repo), inspect the per-task return dict
(`{"kicked": …, "kick_budget_remaining": …, …}`) to determine
whether kicks are being throttled or whether `is_complete` keeps
returning False (the latter would imply rewalks aren't completing —
likely a `backfill_repo_incomplete_prs` rate-limit issue, not an
upgrader bug).

If something looks wrong with synthesized rows specifically, check
that migration 0046's data backfill ran (look for fresh
`REVIEW_APPROVED` / `REVIEW_CHANGES_REQUESTED` / `REVIEW_COMMENTED`
rows whose `created_at` matches the deploy time, with `extra={}`
and `inline_comment_total_count IS NULL`). The live synthesis path
re-runs on every dismiss-event ingest (not just first creation), so
even if the migration somehow missed rows, the wave's rewalks heal
them. The "PRTimelineEvent count grew unexpectedly" question is
expected — it's the synthesized parents.

Things that did **not** change in 5b but next agent might wonder
about:
- The "drop `state=DISMISSED PullRequestReview` at row creation"
  rule still holds (synthesis populates the parent from the dismiss
  event's extra, not from the dismissed PullRequestReview node).
- `PRTimelineEvent` rows are still 1:1 with `timelineItems` nodes
  (the synthesized row's `github_node_id` is the dismissed review's
  real node id; a future walk that surfaces the actual node will
  refresh fields like `inline_comment_total_count` via
  `get_or_create`'s update path).
- `engagement_synced_at` is still being written for rollback
  insurance; remove only after Chunk 6.
- The next ingestion expansion (v=4) would follow the same pattern:
  bump `CURRENT_SYNC_SCHEMA_VERSION`, register an upgrader, ship a
  data-migration reset of `timeline_backfill_done` if a rewalk is
  required. See §Open Questions for the v=4+ candidate list.

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
- **`REVIEW_DISMISSED` ingest synthesizes the dismissed review's parent row.**
  The `state=DISMISSED PullRequestReview` node is dropped at the
  timeline-event creation step (its `state` no longer reveals the original
  submission state — only `previousReviewState` on the dismiss event does).
  But sync-time non-determinism would otherwise produce two distinct data
  shapes for the same logical history depending on when the syncer ran:
  - **Case A** — sync between submission and dismissal: the original
    `REVIEW_<state>` row is created, then later the dismiss event creates
    its `REVIEW_DISMISSED` row. Two rows.
  - **Case B** — first sync only after dismissal: the original
    `PullRequestReview` arrives with `state=DISMISSED` and is dropped, so
    only the `REVIEW_DISMISSED` row is created. One row.

  To make the data shape independent of sync timing, ingesting a
  `REVIEW_DISMISSED` row whose `extra` carries the dismissed review's
  identity + `previous_review_state` ALSO synthesizes the corresponding
  `REVIEW_<previousReviewState>` row, idempotent on the dismissed review's
  `github_node_id`. This is `_synthesize_dismissed_review_parent` in
  `timeline_sync.py`. Side effect: any pre-existing `PRReviewInlineComment`
  rows for that review with `parent_review_event_id=NULL` are linked to
  the new parent. Synthesis runs on every ingest of a dismiss event (not
  just first creation), so the live code self-heals if any production rows
  were missed by the migration's backfill. The 1:1 invariant with
  `timelineItems` nodes is preserved: each synthesized row's
  `github_node_id` corresponds to a real `PullRequestReview` node, and a
  later walk that surfaces the actual node will refresh its fields (e.g.
  `inline_comment_total_count`) via the existing update path.

  Synthesis cannot fire when:
  - `ReviewDismissedEvent.review` was null on GitHub (the dismissed review
    was hard-deleted) — no node id to key on.
  - `previousReviewState` is unexpectedly `PENDING` or `DISMISSED` (not
    possible per GitHub semantics but logged + skipped defensively).

  In the second-rare case where synthesis cannot fire, the dismissed
  review's inline comments still ingest with `parent_review_event=NULL`;
  joins via `review_node_id` keep them queryable, and the
  `PRReviewInlineCommentBackfill` table is also keyed on `review_node_id`
  (not on the parent FK) so the long-tail signal survives.
- **DB-aware thread-root resolution and monotone re-ingest.** The
  inline-comments service computes `thread_root_node_id` by walking
  `replyTo` through the union of (a) the in-flight set of comments under
  any review in the current call and (b) existing `PRReviewInlineComment`
  rows in the DB. When a comment's `replyTo` target leaves the in-flight
  set, the existing row's already-resolved `thread_root_node_id` is
  copied (transitively correct, since prior ingests resolved it). Only
  when neither set has the target do we fall back to the immediate
  `replyTo` id.

  Re-ingest is monotone-toward-truth: a row whose new walk reached a
  definitive root (no fallback) UPSERTs `thread_root_node_id` (so a
  wider-context rewalk improves the stored value); a row whose new walk
  fell back uses INSERT-IGNORE (so a narrower-context rewalk never
  regresses an already-better stored value). Implemented as two
  `bulk_create` calls: `update_conflicts=True` for the definitive batch,
  `ignore_conflicts=True` for the fallback batch.
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

### Chunk 5b. Page-path inline-comments fix + v=3 recovery wave

Chunk 5 shipped with a wire-up gap: `_sync_inline_review_comments`
was only invoked from the bundle path (`sync_pull_request_bundle`).
The two timeline-page loops in `sync_pull_request` —
`get_timeline_page` (forward) and `get_timeline_page_back`
(backward) — called `sync_timeline_events` to persist the parent
`REVIEW_*` / `ISSUE_COMMENTED` rows but did not invoke the
inline-comments service, even though both
`timeline_page.graphql` and `timeline_page_back.graphql` already
embed the nested `comments(first: $inlineCommentsPerReview)`
connection (so the data was on the wire and being discarded).

#### Symptom
- Several thousand PRs at `sync_schema_version=2` with `REVIEW_*` /
  `ISSUE_COMMENTED` rows present but **zero** corresponding
  `PRReviewInlineComment` rows.
- `PRReviewInlineCommentBackfill` table flat-empty even after the
  v=2 wave processed many PRs whose reviews surely had >K inline
  comments somewhere in their history.
- Inline comments only present for PRs whose reviews fit in the
  bundle's `timelineItems(last: $timelineK)` window — the bundle
  path was the only path persisting them.

#### Fix (in this chunk)
1. **Code fix.** `sync_pull_request` now invokes
   `_sync_inline_review_comments(pr_obj, nodes)` after
   `sync_timeline_events(pr_obj, nodes)` in both the forward and
   backward page loops, and accumulates `inline_comments_created` /
   `inline_backfill_rows_upserted` into the result dict. The
   service is bundle-scope-agnostic — it only requires a list of
   timeline nodes — so reusing it page-by-page is correct. Thread-
   root resolution within a page is exact; cross-page replies fall
   back to `reply_to_node_id` per the existing best-effort design
   and reconcile on later rewalks.
   `_apply_assignment_opt_outs` remains called only on the bundle
   and forward paths, **not** the back path: the latest opt-out
   signal lives in recent timeline, never the back-walked tail.

2. **v=3 recovery wave.** PRs already stamped to v=2 will not be
   re-walked under the existing dispatcher rules
   (`is_complete(pr) := pr.timeline_backfill_done` is True for
   them). To re-walk them under fixed code, register `UpgradeToV3`
   (a thin subclass of `UpgradeToV2` with `version=3`) and bump
   `CURRENT_SYNC_SCHEMA_VERSION = 3`. Pair with migration `0045`
   that resets `timeline_backfill_done=False, timeline_backfill_cursor=NULL`
   for every PR at `sync_schema_version<3` — same option (a)
   approach as 0044, applied a second time. Without that reset,
   v=2 PRs would short-circuit `UpgradeToV3.is_complete` to True
   and auto-stamp to v=3 without a rewalk, repeating the v=2-era
   pitfall.

3. **Tests.** New regression file
   `qb_site/syncer/tests/services/test_inline_comments_page_paths.py`
   covers: backward page creates inline-comment rows; backward
   page creates `PRReviewInlineCommentBackfill` marker on
   `hasNextPage=true`; backward page with no review nodes is a
   no-op; forward page creates inline-comment rows. Existing
   `test_timeline_backfill.py` and `test_commit_backfill_synced.py`
   stubs of `sync_pull_request_bundle` updated to include the
   `inline_comments_created` / `inline_backfill_rows_upserted`
   keys (bringing them in line with the canonical bundle return
   shape).

4. **Operational gate (recommended).** Set
   `SYNCER_SCHEMA_UPGRADE_TARGET_VERSION=1` between detecting the
   gap and the 5b deploy. This pauses the v=2 wave so PRs still at
   v=1 are not stamped to a v=2-with-missing-data state during the
   gap window. After the 5b deploy, unset the gate (or set it to
   3) and the wave resumes — paused-at-v=1 PRs go directly v=1 →
   v=3 in a single rewalk under fixed code.

#### Why option (a) again, not (b)
Same reasoning as the original Chunk 5 decision. Option (b) would
require either reusing or adding a `timeline_query_version` /
`inline_comments_query_version` column on `PullRequest` whose only
consumer is this single wave; option (a)'s redundant-rewalk cost
is bounded by `SYNCER_SCHEMA_UPGRADE_KICK_LIMIT`. We've now run
option (a) twice in quick succession; if a third bug discovered
after a v=4 release ever required a third wave, that would be the
moment to invest in option (b) infrastructure.

### Chunk 6. `engagement_synced_at` removal (one release after Chunk 5b)

#### Why no schema-version bump
The schema-version mechanism exists to drive **per-PR rewalks** under
new query/normalizer code so PRs can be brought up to a current data
shape. Chunk 6 does the opposite: it removes plumbing that has been
redundant with `sync_schema_version` since v=1 shipped.

- After Chunks 5 + 5b converge, every PR has `sync_schema_version >= 1`
  and has been re-synced under code that writes `engagement_synced_at`
  on every successful sync (paired with `last_synced_at` in
  `pr_sync_service.py:296,308-314`).
- That makes `engagement_synced_at IS NULL` equivalent to
  `last_synced_at IS NULL` for any PR that has been touched since the
  v=1 deploy. The "missing engagement" cohort the column was originally
  designed to surface no longer exists.
- Every read site of `engagement_synced_at` (the analyzer's
  `_data_status` calls and the `sync_pr_task` skip-decision) needs only
  "has this PR ever been fully synced?" — `last_synced_at` answers that.
- No new GraphQL query, no new normalizer behavior, no new event types.
  Nothing for an upgrader to do. Bumping `CURRENT_SYNC_SCHEMA_VERSION`
  would only schedule pointless rewalks.

So Chunk 6 is a code/schema cleanup, gated only on time (one release of
soak after 5b) and on the v=3 wave fully converging on each active repo
(`prs_below_current_sync_schema_version=0`).

#### Deploy 6a — stop writing, retire the engagement-backfill task
Pure code change + one deploy. No migration. Reversible by redeploying
previous code.

**File-by-file:**

1. `qb_site/syncer/services/pr_sync_service.py` — drop the two writes:
   - Remove `extras["engagement_synced_at"] = now_ts` (currently line
     ~296).
   - Remove the trailing fallback `if "engagement_synced_at" not in
     update_fields: pr_obj.engagement_synced_at = now_ts;
     update_fields.append("engagement_synced_at")` (lines ~308-310).
   - The comment about "advance `last_synced_at` after engagement
     fields are prepared" can be slightly trimmed but is still
     correct — the relevant invariant is now "after assignees + files
     + timeline are saved", not "after engagement".

2. `qb_site/syncer/tasks/sync_tasks.py` — drop the redundant
   skip-bypass:
   - Remove `needs_engagement = bool(pr_db and pr_db.engagement_synced_at is None)`
     and its use in the skip predicate (lines ~187, 206).
   - The `last_synced_cutoff` precondition already gates the skip
     branch on "we have a prior sync"; without that, the skip never
     fires. With it, `engagement_synced_at` is non-null. So
     `needs_engagement` is dead weight today.

3. `qb_site/syncer/tasks/backfill_tasks.py` — delete the engagement
   backfill task entirely (lines ~291-380):
   - `backfill_repo_engagement_task` (`syncer.backfill_repo_engagement`).
   - `backfill_repo_engagement_active_task`
     (`syncer.backfill_repo_engagement_active`).
   - The `head_sha`/`head_ci_state` clauses that share this task are
     redundant with `backfill_repo_incomplete_prs`'s
     `head_ci_state__iexact='PENDING'` and `last_synced_at__isnull`
     filters plus the v=2/v=3 wave's full re-walks; we don't need a
     parallel scan for them.

4. `qb_site/syncer/tasks/collect_convergence.py` — stop computing the
   engagement metrics:
   - Remove `engagement_missing` and `engagement_incomplete` (lines
     ~74-81).
   - Remove `prs_missing_engagement=engagement_missing` and
     `prs_engagement_incomplete=engagement_incomplete` from the
     `SyncerConvergenceSnapshot.objects.create(...)` call.
   - Remove them from the `per_repo` dict.
   - Note: this leaves the model fields in place for one release; they
     receive a default `0` write (the model defaults are `0`) until 6b
     drops the columns. That's intentional rollback insurance.

5. `qb_site/qb_site/settings/base.py` — drop the engagement-backfill
   plumbing:
   - Lines ~237-238 in `CELERY_TASK_ROUTES`: remove the two
     `syncer.backfill_repo_engagement[_active]` queue routes.
   - Lines ~294-295: remove `SYNCER_ENGAGEMENT_BACKFILL_PERIOD_SECONDS`
     and `SYNCER_ENGAGEMENT_BACKFILL_LIMIT`.
   - Lines ~524-532: remove the `if SYNCER_ENGAGEMENT_BACKFILL_PERIOD_SECONDS > 0`
     beat-schedule block.

6. `.env.example` — drop the two `SYNCER_ENGAGEMENT_BACKFILL_*`
   entries (lines ~185-187) and any preceding comment.

7. `qb_site/syncer/AGENTS.md` — drop the
   `syncer.backfill_repo_engagement_active → syncer.backfill_repo_engagement`
   bullet from the Beat schedule list.

8. `qb_site/analyzer/services/queueboard_snapshot.py` — switch the
   four `_data_status` callers (lines ~953-956) from
   `pr.engagement_synced_at` to `pr.last_synced_at`. `_data_status`
   uses the timestamp only as a "missing" sentinel (None ⇒ never
   synced), so the substitution is exact: `pr_sync_service.py` writes
   both fields on the same successful-sync path, so any PR with
   `last_synced_at` set already had `engagement_synced_at` set under
   the old code. There is no semantic regression.

9. **Tests to update / delete:**
   - `qb_site/syncer/tests/backfill/test_engagement_backfill_task.py`
     — delete the file (the task is gone).
   - `qb_site/syncer/tests/tasks/test_collect_convergence_task.py` —
     drop the `engagement_synced_at=...` constructor kwargs and the
     two `prs_missing_engagement` / `prs_engagement_incomplete`
     assertions.
   - `qb_site/syncer/tests/tasks/test_sync_pr_task_skip.py` and
     `qb_site/syncer/tests/backfill/test_commit_backfill_only.py` and
     `qb_site/syncer/tests/backfill/test_sync_pr_backfill_only.py` —
     drop the `pr.engagement_synced_at = last_synced_at` setup
     (`last_synced_at` alone is what gates the skip path now).
   - `qb_site/syncer/tests/test_pr_sync_integration.py` — remove the
     `assertIsNotNone(pr.engagement_synced_at)` assertion.
   - `qb_site/analyzer/tests/tasks/test_collect_convergence_task.py`,
     `qb_site/analyzer/tests/test_queueboard_snapshot.py`,
     `qb_site/analyzer/tests/services/test_reviewer_assignment.py`,
     `qb_site/analyzer/tests/services/test_dependency_graph.py` —
     drop `engagement_synced_at=...` constructor kwargs / `.save(...)`
     calls in test setup.
   - Search-and-prune any remaining references with
     `grep -rn engagement_synced_at qb_site/`.

10. `qb_site/syncer/admin.py` — leave `engagement_synced_at` in the
    PR admin's `readonly_fields` and the two snapshot fields in
    `list_display` / `readonly_fields` for the 6a release. Remove
    them in 6b alongside the column drop. (Admin will display the
    last-frozen value during the 6a soak; this is the intended
    rollback observability.)

**6a soak watch list:**
- `engagement_synced_at` values frozen across the deploy boundary on
  spot-checked PRs (no further increments). New PRs created after
  6a have `engagement_synced_at=NULL` — that's expected and benign.
- `prs_missing_engagement` in fresh `SyncerConvergenceSnapshot` rows
  trends to a stable count (the count of new PRs since 6a) rather
  than zero; we'll drop the metric in 6b.
- `backfill_repo_engagement[_active]` no longer appears in the Celery
  beat schedule and no longer fires.
- `analyzer.queueboard_snapshot` produces unchanged `_data_status`
  values for a sample of PRs (since we swapped the input timestamp,
  not the predicate). Spot-check one open PR per active repo.
- `bash scripts/repo_check_compose.sh` green.

**6a rollback:** redeploy previous code. The column still exists and
nothing has been destructively migrated, so the rollback target picks
up where it left off. The only side effect is a small cohort of new
PRs with `engagement_synced_at=NULL` — `backfill_repo_engagement` will
re-fill them once it's running again.

#### Deploy 6b — drop the columns and the snapshot metrics
Lands at least one release after 6a, and only after a fresh
`SyncerConvergenceSnapshot` confirms no live writers (other than
default-0 writes) and no operator dashboards still surface the
fields.

**Migration `0047_drop_engagement_synced_at_and_snapshot_metrics`:**

```python
# qb_site/syncer/migrations/0047_drop_engagement_synced_at_and_snapshot_metrics.py
operations = [
    migrations.RemoveField("pullrequest", "engagement_synced_at"),
    migrations.RemoveField("syncerconvergencesnapshot", "prs_missing_engagement"),
    migrations.RemoveField("syncerconvergencesnapshot", "prs_engagement_incomplete"),
]
```

Postgres will issue three `ALTER TABLE ... DROP COLUMN` statements;
DROP COLUMN on a Postgres table is `O(1)` metadata + lazy reclaim, so
locking posture is brief even on the multi-million-row PR table.

**File-by-file accompanying changes:**

1. `qb_site/syncer/models/pull_request.py` — drop the
   `engagement_synced_at = models.DateTimeField(null=True, blank=True)`
   field (line 69).

2. `qb_site/syncer/models/convergence_snapshot.py` — drop the two
   `prs_missing_engagement` / `prs_engagement_incomplete` fields
   (lines 30-31).

3. `qb_site/syncer/admin.py` — remove `engagement_synced_at` from
   the PR admin (line 167) and `prs_missing_engagement` /
   `prs_engagement_incomplete` from the `SyncerConvergenceSnapshot`
   admin's `list_display` and `readonly_fields` (lines 1315-1316,
   1340-1341).

4. `scripts/backup_policy.py` — drop the columns from any
   field-level allow-list. Run `bash scripts/repo_check_compose.sh`
   to confirm the backup-policy validator stays green.

5. `docs/queueboard_api_contract.md` — remove the
   `engagement_synced_at` mentions on lines 92, 107, 126, 130
   (replace with `last_synced_at` where the meaning was "has the
   PR been fully synced?"). Also remove the
   `syncer.backfill_repo_engagement` reference on line 107 and the
   `SYNCER_ENGAGEMENT_BACKFILL_*` env-var reference.

6. `docs/design-decisions/017-token-cost-tracking.md` — remove the
   `backfill_repo_engagement(_active)` entry from the
   "enqueue-only / DB-only tasks" list (line 15).

**6b soak / verification:**
- Migration applies cleanly on a recent prod snapshot (or a staging
  copy) before deploying.
- `bash scripts/repo_check_compose.sh` green; the
  `validate_backup_policy.py` step in particular confirms no
  policy-coverage gap.
- Post-deploy: latest `SyncerConvergenceSnapshot` row no longer
  carries `prs_missing_engagement` / `prs_engagement_incomplete`
  (admin and the periodic task both behave). PR admin no longer
  surfaces `engagement_synced_at`.

**6b rollback:** the column drops are irreversible without a
backfill from a backup. By the time 6b ships, no remaining writer
or reader exists, so the only realistic regression mode is "we
discover an external consumer (a downstream report, an analytics
notebook) that still queried `engagement_synced_at`." Mitigation:
search outside the repo (operator dashboards, ad-hoc Metabase
queries, the Zulip bot) before 6b deploy and verify nothing reads
the column.

#### Optional follow-up (not scheduled)
- **Soft-deprecation of `commenters` / `approvals` / `number_total_comments`.**
  These aggregate fields on `PullRequest` are noted in §Subtleties as
  "soft-deprecated, prefer event log". They are still computed from
  the bundle's `reviews(first: 100)` / `comments(first: 100)`
  connections at v=3 and continue to have consumers
  (`queueboard_snapshot`, reports). Switching their computation to
  `PRTimelineEvent` aggregates over the event log is a separate v=4+
  task; it does not block Chunk 6.

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
- **Migration locking posture at scale.** Migrations 0044 / 0045 issue
  `PullRequest.objects.filter(...).update(...)` — single bulk SQL
  UPDATE. On the current PR table this completes quickly (verified
  empirically: 0044 ran cleanly on production at the v=2 deploy). On a
  multi-million-row table Postgres takes a row-level write lock on every
  matched row for the duration of the UPDATE, which can briefly block
  concurrent `sync_pr_task` writes. Migration 0046 issues two bulk SQL
  statements (synthesis INSERT...SELECT and a UPDATE...FROM); the
  synthesis is bounded by the population of `REVIEW_DISMISSED` rows
  (small) and the FK-link UPDATE is bounded by inline comments with
  null parent FK (also small in practice). **If the PR table grows
  another order of magnitude before v=4, chunk these by
  `repository_id` or `id` ranges with a `RunPython` loop.**
- **Post-deploy shape checks (run on prod after each deploy boundary).**
  These are positive-signal SQL queries: rerun them at intervals after
  the deploy and confirm the expected transitions actually happen.
  A query that "obviously should return non-zero but doesn't" is the
  signal that an ingestion path is silently dropping data even though
  the GraphQL response carries it (this is how the v=2 inline-comments
  gap manifested — see §Chunk 5b).
  - **After Chunks 4a–4d (the per-PR-sync ingestion):**
    ```sql
    -- New event types must start appearing on fresh syncs.
    SELECT type, COUNT(*)
      FROM syncer_prtimelineevent
      WHERE created_at >= now() - interval '1 day'
        AND type IN ('ISSUE_COMMENTED', 'REVIEW_APPROVED',
                     'REVIEW_CHANGES_REQUESTED', 'REVIEW_COMMENTED',
                     'REVIEW_DISMISSED', 'REVIEW_REQUESTED',
                     'REVIEW_REQUEST_REMOVED')
      GROUP BY type;
    -- Inline comments under those reviews must also appear.
    SELECT COUNT(*) FROM syncer_prreviewinlinecomment
      WHERE created_at >= now() - interval '1 day';
    ```
    Both queries must return non-zero within a sync cycle of the
    deploy. **A non-zero `PRTimelineEvent` count alongside a zero
    `PRReviewInlineComment` count is the v=2 gap signature**; if you
    see it, the page-loop wire-up has regressed.
  - **After Chunk 5 / 5b (the wave):**
    ```sql
    -- Wave progress: must trend monotonically up over days.
    SELECT repository_id, COUNT(*)
      FROM syncer_pullrequest
      WHERE sync_schema_version = (SELECT MAX(sync_schema_version_target)
                                     FROM syncer_syncerconvergencesnapshot)
      GROUP BY repository_id;
    -- Long-tail backfill markers must start appearing once the wave
    -- has rewalked PRs whose reviews have >SYNCER_INLINE_COMMENTS_PER_REVIEW
    -- inline comments. Empty after Chunks 4a–4d alone (those PRs
    -- haven't been re-walked); non-empty once the wave has covered
    -- representative repos.
    SELECT COUNT(*) FROM syncer_prreviewinlinecommentbackfill;
    -- Cross-check: PRs at v=N must have inline comments captured.
    -- A v=N PR with REVIEW_* events but zero PRReviewInlineComment
    -- rows AND a parent review with comments.totalCount > 0 is the
    -- gap signature.
    SELECT COUNT(*)
      FROM syncer_pullrequest pr
      WHERE pr.sync_schema_version >= 3
        AND EXISTS (
          SELECT 1 FROM syncer_prtimelineevent ev
            WHERE ev.pull_request_id = pr.id
              AND ev.type IN ('REVIEW_APPROVED', 'REVIEW_CHANGES_REQUESTED',
                              'REVIEW_COMMENTED')
              AND COALESCE(ev.inline_comment_total_count, 0) > 0
        )
        AND NOT EXISTS (
          SELECT 1 FROM syncer_prreviewinlinecomment ic
            WHERE ic.pull_request_id = pr.id
        );
    ```
    The third query should return 0 (or near-0; rare PRs may have
    inline comments only in deleted-review states). A non-trivial
    count is a regression signal.

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
7. **Chunk 5b → deploy.** Page-path inline-comments fix + v=3
   recovery wave. Bumps `CURRENT=3`, registers `UpgradeToV3`, and
   ships migration `0045` that resets `timeline_backfill_done` for
   every PR at v<3. While 5b is in flight (between detecting the
   v=2 gap and the 5b deploy), the recommended posture is to set
   `SYNCER_SCHEMA_UPGRADE_TARGET_VERSION=1` to halt the v=2 wave
   and prevent more PRs from being stamped to v=2-with-missing
   inline comments. The same wave then converges everyone (paused
   v=1 PRs and already-v=2 PRs) to v=3 under fixed code.
8. **Chunk 6a → deploy** (post-soak after 5b). Stop writing
   `engagement_synced_at`; delete `backfill_repo_engagement[_active]`
   tasks, their beat schedule, their settings, and their queue
   routes; switch `analyzer.queueboard_snapshot._data_status`
   callers to `last_synced_at`; drop `prs_missing_engagement` /
   `prs_engagement_incomplete` from `collect_convergence`'s writes
   (snapshot columns retained for one release of rollback insurance).
   Pure code change, no migration, fully reversible.
9. **Chunk 6b → deploy** (one release after 6a). Migration
   `0047` drops the `engagement_synced_at` column and the two
   `SyncerConvergenceSnapshot` engagement metrics; admin/backup
   policy/API-contract docs updated in lockstep. Postgres
   `DROP COLUMN` is metadata-only so the lock window is brief even
   on a multi-million-row PR table.

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
- Should the v2/v3 upgrader live as its own Celery task or be folded
  into `backfill_repo_incomplete_prs`? Folding is simpler; separate
  gives cleaner observability per upgrade wave.
- Whether to keep writing `engagement_synced_at` across the deprecation
  window for rollback insurance, or stop writing it immediately and rely
  on `sync_schema_version >= 1`.
- `MERGED` event in v=4: bundled with whatever the next expansion is,
  or its own version bump?

### v=4+ candidates (deferred from Chunk 5b review)
These were surfaced during the Chunk 5b review (see Progress Notes
2026-05-08) as data-quality opportunities that would benefit from a
future schema-version expansion. None are blocking for v=3.

- **`PRReviewInlineCommentBackfill` consumer.** The marker table is now
  written reliably (Chunk 5b) but has no consumer — rows accumulate
  without bound. v=4 would add the actual paginator that walks
  `PullRequestReview.comments` past the K=20 cutoff and clears or marks
  rows complete.
- **Thread-root reconciliation BEYOND the in-bundle + DB walk.** Item 2
  fixes the common case (cross-page chains converge via DB lookups and
  monotone UPSERT). It does NOT defend against a hypothetical scenario
  where intermediate rows in the chain were deleted from the DB after
  ingest — a re-ingest of a leaf would then fall back to a worse value,
  but the INSERT-IGNORE branch leaves the existing (better) stored
  value alone. The leaf's stored root might still be "stale-better"
  rather than "current-truth." A future principled fix could
  periodically re-resolve thread roots from the durable
  `reply_to_node_id` graph.
- **Promoting `extra.previous_review_state` to a typed column.** The
  Chunk 5b synthesis approach reads `previous_review_state` from JSON
  on every dismiss-event ingest. If reviewer-engagement queries grow
  to filter on it, a typed column would index-friendlier — minor
  optimization.
- **`MERGED` event capture** (already noted above).
- **Capture the dismissed `PullRequestReview` row's
  `inline_comment_total_count` from the actual node, not from the
  dismiss event.** Today the synthesized row's count is null until
  the actual `PullRequestReview` (state=DISMISSED) is later walked.
  We could update this on the inline-comments service side too, but
  it's small.

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

- 2026-05-08: **Chunk 5 implemented.** Picked option (a) (data-migrate
  reset of `timeline_backfill_done`) over option (b) (track
  `timeline_query_version`) per the design recommendation: option (b)
  would have added a column on `PullRequest` whose only consumer is
  this single wave, and the redundant-rewalk cost option (a) imposes
  is bounded by `SYNCER_SCHEMA_UPGRADE_KICK_LIMIT` so the worst case
  is a paced re-walk rather than a thundering herd. `UpgradeToV2`
  lives in its own module
  (`syncer/services/sync_schema_upgrade_v2.py`) and is registered via
  `SyncerConfig.ready()`; `register_v2_upgrader` is idempotent so
  test fixtures and re-imports don't trip the registry duplicate
  guard. Added the optional `SYNCER_SCHEMA_UPGRADE_TARGET_VERSION`
  gate (clamped to `CURRENT_SYNC_SCHEMA_VERSION` on the upper side)
  and threaded it through the candidate-filter query and the
  per-PR dispatcher target. Pre-existing tests that hard-coded
  `CURRENT=1` were updated to reflect `CURRENT=2`; new tests cover
  the gate, the v2 kick (`force=True, backfill_timeline_pages` from
  settings), and `is_complete` true/false. The kick passes
  `backfill_timeline_pages=SYNCER_TIMELINE_BACKFILL_PAGES` (not 0) so
  it does meaningful rewalk work on first invocation rather than
  just a head sync — interpreting the original design's "so the
  dedupe doesn't swallow the rewalk" phrase as the kick itself being
  the rewalk vehicle.
- 2026-05-08: **4a–4c deployed; 4d implemented.** Live ingestion is
  producing the new `PRTimelineEvent` types and `PRReviewInlineComment`
  rows. `PRReviewInlineCommentBackfill` is still empty as predicted —
  the threshold for a backfill row is a single `PullRequestReview` with
  `>SYNCER_INLINE_COMMENTS_PER_REVIEW` (=20) inline comments, and the
  long-tail population of such reviews lives on PRs that won't be
  re-walked until the Chunk 5 wave fires. Pre-flight SQL against prod
  returned 0 rows for each of the three Chunk 4d constraint queries
  (`bad_routing`, `not_mutex`, `bad_inline_total`), confirming current
  ingestion conforms. Migration `0043` + negative tests committed; the
  next deploy enables them.
- 2026-05-08: **Chunk 5 deployed; gap detected; Chunk 5b implemented.**
  Live observation: several thousand PRs at `sync_schema_version=2`
  have new `REVIEW_*` / `ISSUE_COMMENTED` rows but no
  `PRReviewInlineComment` rows. Cause: `_sync_inline_review_comments`
  was only invoked from `sync_pull_request_bundle`; the forward
  (`get_timeline_page`) and backward (`get_timeline_page_back`) page
  loops in `sync_pull_request` called `sync_timeline_events` only,
  even though both queries embed `comments(first: K)`. The v=2 wave's
  rewalks therefore created the parent rows but discarded the inline
  comments. Implemented Chunk 5b on the same branch:
  1. Wired `_sync_inline_review_comments` into both page loops in
     `pr_sync_service.py` and accumulated the new counters into the
     result dict.
  2. Added `UpgradeToV3` (subclass of `UpgradeToV2`, `version=3`)
     and migration `0045` resetting `timeline_backfill_done` for
     every PR at v<3. Bumped `CURRENT_SYNC_SCHEMA_VERSION = 3` and
     registered v3 in `SyncerConfig.ready()`.
  3. Added `tests/services/test_inline_comments_page_paths.py`
     with backward-page + forward-page regression coverage and
     `tests/services/test_sync_schema_upgrade_v3.py` mirroring the
     v2 tests. Updated CURRENT=2 expectations in
     `test_collect_convergence_task.py`,
     `test_upgrade_schema_tasks.py`, and
     `test_sync_schema_upgrades.py`.
  4. Updated `qb_site/syncer/AGENTS.md` with a "Timeline ingest
     invariants" subsection codifying the bundle / forward-page /
     back-page invariant so future nested-data ingest expansions
     don't repeat the gap.
  Operational recommendation: set
  `SYNCER_SCHEMA_UPGRADE_TARGET_VERSION=1` between gap detection
  and 5b deploy to halt the v=2 wave; this prevents the affected
  cohort from growing during the gap window. After 5b deploys,
  unset the gate (or set it to 3) and the wave resumes; PRs paused
  at v=1 advance directly to v=3 in a single rewalk under fixed
  code.
- 2026-05-08: **Chunk 5b extended with two consistency fixes** after
  branch review surfaced subtler issues than the original wire-up gap:
  1. **Item 1 — DISMISSED review parent synthesis.** A
     `state=DISMISSED PullRequestReview` is dropped at row creation
     because its current `state` field doesn't reveal the original
     submission state. But that meant the same logical history
     produced two different DB shapes depending on sync timing
     (Case A: synced before dismissal → `REVIEW_<state>` row exists +
     dismiss event row; Case B: only synced after → only the dismiss
     event row, with `parent_review_event=NULL` on inline comments).
     Fixed by synthesizing the `REVIEW_<previousReviewState>` row from
     the dismiss event's denormalized `extra` data, idempotent on the
     dismissed review's `github_node_id`. See
     `_synthesize_dismissed_review_parent` in `timeline_sync.py` and
     §Subtleties. Migration `0046` backfills existing production data.
     Schema tightening on the same migration: `PRReviewInlineCommentBackfill`
     now keys on `review_node_id` (unique) with a nullable
     `review_event` FK, so the long-tail signal survives even when
     synthesis can't fire (dismiss event with `review: null`); a
     `review_node_id != ''` CHECK constraint on `PRReviewInlineComment`
     defends against accidental orphan rows; an index on
     `reply_to_node_id` supports the new DB-aware thread-root lookup.
  2. **Item 2 — DB-aware thread-root walk + monotone UPSERT.** Once
     Chunk 5b made the inline-comments service run page-by-page
     (instead of bundle-scope), the in-flight set shrunk and
     cross-page thread replies started falling back to immediate
     `replyTo` instead of resolving to the true root. Fixed by
     extending the walk to consult existing `PRReviewInlineComment`
     rows when a `replyTo` target leaves the in-flight set, and
     replacing `bulk_create(ignore_conflicts=True)` with a split
     two-batch upsert: rows whose new walk reached a definitive root
     UPSERT `thread_root_node_id` (so wider-context rewalks improve
     stored values), rows whose walk fell back stay INSERT-IGNORE (so
     narrower-context rewalks never regress already-better values).
     New tests: `TestThreadRootDBAwareResolution` in
     `test_inline_comments_sync.py` covers single-link, three-link
     chain, fallback preservation, and complete-walk upsert.

  Also added in this batch (non-correctness-affecting):
  - Comment near `pr_sync_service.py:404` documenting the
    "webhook-flips-`timeline_backfill_done`-back-to-True" race
    assumption and what would invalidate it.
  - Auto-stamp log line dropped from INFO to DEBUG (a wave can produce
    millions of these).
  - Migration locking posture documented in §Validation Plan as an
    operational note (current bulk UPDATEs are fine; chunk if the PR
    table grows another OoM).
  - v=4+ candidates listed in §Open Questions to capture the data-
    quality opportunities surfaced during this review.

- 2026-05-09: **Chunk 5b soak observation; Chunk 6 plan finalized.**
  After ~24 hours of soak the v=3 wave is converging cleanly across
  active repos: `prs_below_current_sync_schema_version` trends
  monotonically downward in fresh `SyncerConvergenceSnapshot` rows,
  `PRReviewInlineComment` rows grow in lockstep, and
  `PRReviewInlineCommentBackfill` markers now appear for the
  long-tail reviews predicted by Phase 0. No regressions surfaced
  in admin/queueboard snapshots or in the rate-limit telemetry.

  Confirmed Chunk 6 needs no schema-version bump: the original
  `engagement_synced_at` column was a "have we ever populated this
  PR's engagement fields?" sentinel that has been redundant with
  `sync_schema_version >= 1` (and with `last_synced_at` for the
  read paths) since Chunk 1 shipped. Removing it is a code/schema
  cleanup, not an ingestion expansion; nothing for an upgrader to
  run. See §Chunk 6 for the rationale and the file-by-file
  6a / 6b plan.

- 2026-05-10: **Chunk 6b implemented.** Migration
  `0047_drop_engagement_synced_at_and_snapshot_metrics` regenerated cleanly
  via `uv run python qb_site/manage.py makemigrations syncer` after dropping
  `PullRequest.engagement_synced_at` and the two
  `SyncerConvergenceSnapshot.prs_{missing,engagement_incomplete}_*` fields
  from their model files. `qb_site/syncer/admin.py` updated in lockstep
  (PR admin `readonly_fields`; convergence-snapshot admin `list_display`
  + `readonly_fields`). `docs/queueboard_api_contract.md` updated to
  reference `last_synced_at` / `sync_schema_version` instead of
  `engagement_synced_at`, and the `syncer.backfill_repo_engagement[_active]`
  / `SYNCER_ENGAGEMENT_BACKFILL_*` mentions replaced with the active
  backfill mechanism (`backfill_repo_incomplete_prs[_active]` +
  `upgrade_schema_versions[_active]`). `docs/design-decisions/017-token-cost-tracking.md`'s
  enqueue-only-tasks list trimmed of `backfill_repo_engagement(_active)`.
  `scripts/backup_policy.py` required no edit: it has no field-level
  allowlist, and all retained tables export via `SELECT *`, so dropped
  columns are reflected automatically by Postgres. Pre-deploy check is
  `bash scripts/repo_check_compose.sh` (covers ruff, Django tests, the
  `validate_backup_policy.py` step). Migration applies cleanly on a recent
  prod snapshot before the deploy proper; `DROP COLUMN` on the
  multi-million-row PR table is metadata-only so the lock window is brief.

## Finalization Notes
- After v2 ships and `engagement_synced_at` is dropped, convert this doc into
  a concise final-decision record describing: the upgrader framework, the set
  of event types captured at v2, `PRReviewInlineComment`, and the deprecated
  mechanism. Move chunked rollout details to git history.
