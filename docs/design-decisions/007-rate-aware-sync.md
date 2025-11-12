# Rate-Aware Repo Sync Orchestration

## Context
- A single GitHub GraphQL token is shared across Celery workers. We need to avoid overruns when multiple repo sync tasks run concurrently and stop early when the token budget is low.

## Decision
- Store the most recent `rateLimit` snapshot returned by GitHub into Redis.
- Repo-level sync (`syncer.sync_repo_since`) consults the snapshot after discovery and, if `remaining <= SYNCER_RATE_REMAINING_MIN`, stops early and schedules a continuation at `resetAt + jitter`.
- Debounce the continuation with a Redis SETNX keyed by repo+resetAt to prevent duplicate schedules across processes.

## Flow

```
[beat] -> sync_active_repos  ->  sync_repo_since(repo)
                              |        
                              |  GraphQL discovery
                              v        
                         rate snapshot  ---> Redis (set)
                              |
                      remaining <= threshold?
                        |                 \
                       yes                 no
                        |                   \
             schedule continuation            enqueue per-PR sync tasks
                at resetAt+jitter
```

## Rationale
- Redis is already in the stack (Celery broker); it provides cross-process visibility, TTLs, and atomic SETNX to coordinate safely.
- State remains minimal: no persisted cursors. If a run ends early, idempotency + repeated windows resume cleanly after reset.

## Configuration
- `SYNCER_RATE_REMAINING_MIN` (pause threshold).
- `SYNCER_ACTIVE_REPOS_PERIOD_SECONDS` (beat period).
- Other SYNCER_* knobs unchanged.

## V1.1 Enhancements (Implemented)
- Batch enqueue sizing: repo task enqueues up to `min(batch_max, floor((remaining-threshold)/est_cost))` PRs to make progress without overspending.

## Future
- Optional global concurrency controls (single-flight lock or Redis semaphore) if we run multiple workers.
