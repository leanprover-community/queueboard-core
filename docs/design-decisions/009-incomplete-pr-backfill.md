# Incomplete PR Backfill Design

## Context
- Syncer currently has three pillars:
  - Incremental discovery via `syncer.sync_repo_since` using `GitHubClient.get_changed_pr_numbers(owner, name, since_iso, states)` with a sliding `SYNCER_DISCOVERY_LOOKBACK_MINUTES`.
  - Per-PR ingestion via `syncer.sync_pr` with a single watermark `PullRequest.last_synced_at` and optional timeline/commit backfill:
    - `timeline_backfill_done`, `timeline_backfill_cursor`, `timeline_earliest_synced_at`
    - `commits_backfill_done`, `commits_backfill_cursor`, `commits_earliest_synced_at`
  - Historical PR backfill via `syncer.backfill_repo_history_task` using `GitHubClient.get_prs_created_page` ordered by `CREATED_AT ASC` and `syncer.RepoBackfillCursor`.
- Remaining gap this design addresses:
  - PRs that exist in `syncer.PullRequest` but still have `timeline_backfill_done=False` or `commits_backfill_done=False` may otherwise never be fully backfilled if:
    - They are rarely updated (fall outside the discovery lookback window).
    - They were only partially backfilled before a rate-limited run or downtime.
  - Analyzer’s `PRRevision` windows and CI across force-pushes assume timeline/commit backfill is complete; incompleteness can lead to missing revision windows or missing CI for historical heads.

The incomplete-PR backfill service closes this gap by walking the set of known PRs whose backfill flags are still false and gradually driving them toward completeness.

## Incomplete PR Backfill Task: `backfill_repo_incomplete_prs_task`

### Shape (per-repository Celery task)
- Implementation lives in `qb_site/syncer/tasks/backfill_tasks.py`:

  ```python
  @shared_task(name="syncer.backfill_repo_incomplete_prs")
  def backfill_repo_incomplete_prs_task(
      repo_id: int,
      *,
      limit: int = 50,
      states: Optional[Sequence[str]] = None,
  ) -> Dict[str, Any]:
      ...
  ```

### Selection
- The task queries `syncer.PullRequest` for PRs that appear incomplete:

  ```python
  from django.db.models import Q
  from syncer.models import PullRequest

  qs = PullRequest.objects.filter(repository=repo)
  # Optional GitHub-style state filter (OPEN, MERGED, CLOSED) mapped to local
  # DB states: OPEN -> "open"; CLOSED/MERGED -> "closed".
  if db_states is not None:
      qs = qs.filter(state__in=list(db_states))
  qs = qs.filter(
      Q(timeline_backfill_done=False) |
      Q(commits_backfill_done=False)
  )
  ```

- Order for stable progress:
  - Reasonable default: `order_by("-gh_updated_at", "-id")` to prioritize recently updated PRs that are still incomplete.
- Limit:
  - Take only `limit` PRs per run to bound work:

    ```python
    candidates = list(qs.order_by("-gh_updated_at", "-id")[:limit])
    ```

### Enqueue behavior
- For each candidate PR, the task enqueues a normal `sync_pr_task` with the configured backfill budgets:

  ```python
  from django.conf import settings
  from syncer.tasks.sync_tasks import sync_pr_task

  backfill_timeline_pages = int(getattr(settings, "SYNCER_TIMELINE_BACKFILL_PAGES", 0))
  backfill_commit_pages = int(getattr(settings, "SYNCER_COMMITS_BACKFILL_PAGES", 0))

  for pr in candidates:
      sync_pr_task.delay(
          repo.id,
          int(pr.number),
          backfill_timeline_pages=backfill_timeline_pages,
          backfill_commit_pages=backfill_commit_pages,
      )
  ```

- It relies on existing `sync_pr_task` semantics:
  - Header preflight may short-circuit truly up-to-date PRs.
  - Timeline/commit backfill budgets drive `*_backfill_done` flags toward `True`.
  - Calls are idempotent and safe to repeat.

### Output

```python
return {
    "repo": f"{repo.owner}/{repo.name}",
    "repo_id": repo.id,
    "enqueued": len(candidates),
    "remaining": max(total_incomplete - len(candidates), 0),
    "states": states or ["OPEN", "MERGED", "CLOSED"],
}
```

## Interaction with Existing Backfill/Discovery

### Created-at backfill (`backfill_repo_history_task`)
- Responsibility: ensure **every PR number** in the repo is seen and synced at least once.
- Source of truth: GitHub, via `get_prs_created_page` (ordered by `CREATED_AT ASC`).
- State: `syncer.RepoBackfillCursor`.
- Complementary to incomplete-PR backfill:
  - Once a PR is created and `backfill_repo_history_task` has enqueued a `sync_pr` for it, `backfill_repo_incomplete_prs_task` can take over to ensure its timeline/commits are fully backfilled.

### Sliding discovery (`sync_repo_since_task`)
- Responsibility: keep **recently updated** PRs fresh based on `updatedAt`.
- Source of truth: GitHub, via `get_changed_pr_numbers`.
- Sliding window may miss:
  - Updates that occurred during long downtimes and are now older than the lookback window.
  - Rarely updated PRs where timeline/commit backfill never fully completed.

### Incomplete PR backfill task
- Responsibility: drive DB-side completeness flags to `True` over time for already-known PRs.
- Source: DB (`PullRequest` rows) rather than GitHub listing.
- Behavior:
  - Independent of `createdAt` and `updatedAt` ordering; just walks the set of incomplete PRs gradually.
  - Scheduled periodically (default hourly) per active repo with a small `limit` to avoid bursts.

## Analyzer Considerations
- Analyzer’s revision and CI backfill flows assume:
  - `timeline_backfill_done == True` before calling `rebuild_pr_revisions(pr)`.
  - `commits_backfill_done == True` when using `next_revision_backfill_shas(pr, ...)` to find missing CI heads across force-pushes.
- The incomplete PR backfill task will:
  - Reduce the number of PRs where `rebuild_pr_revisions` must be skipped due to unfinished timeline backfill.
  - Improve confidence that commit backfill across the PR’s lifetime is done before Analyzer runs CI-by-SHA backfills.

## Operational Notes and Tradeoffs
- Placement:
  - `backfill_repo_incomplete_prs_task` lives in `qb_site/syncer/tasks/backfill_tasks.py`.
  - A coordinator task `backfill_repo_incomplete_prs_active_task` is exposed as Celery task `syncer.backfill_repo_incomplete_prs_active` and iterates active repos with a small `limit`.
- Admin & CLI:
  - Repository “Sync tools” page includes a button “Enqueue incomplete-PR backfill” which enqueues `backfill_repo_incomplete_prs_task` for that repo.
  - Repository admin changelist includes an action “Enqueue incomplete-PR backfill for selected repositories”.
  - Management command `backfill_incomplete_prs` runs or enqueues incomplete-PR backfill per repo.
- Scheduling:
  - Celery beat schedules `syncer.backfill_repo_incomplete_prs_active` every `SYNCER_INCOMPLETE_BACKFILL_PERIOD_SECONDS` with a per-repo `limit` of `SYNCER_INCOMPLETE_BACKFILL_LIMIT`.
  - `SYNCER_TIMELINE_BACKFILL_PAGES` / `SYNCER_COMMITS_BACKFILL_PAGES` bound per-PR work; the incomplete-PR backfill uses these budgets when calling `sync_pr_task`.
- Tradeoffs and open questions:
  - We still only track `*_backfill_done` as booleans; we may later add counters or “last backfilled at” timestamps to distinguish “never attempted” from “in progress”.
  - Once most PRs are complete, we may want to reduce `SYNCER_INCOMPLETE_BACKFILL_PERIOD_SECONDS` (run less often) or prioritize older/high-value PRs.
  - Exposing incomplete-PR counts (e.g., on the Repository tools page or via metrics snapshots) would make it easier to see convergence and tune limits.

