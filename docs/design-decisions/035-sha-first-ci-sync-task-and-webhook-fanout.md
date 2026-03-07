# SHA-First CI Sync Task And Webhook Fanout

## Context
- Check-event webhook processing previously resolved PR numbers first and enqueued CI sync work per PR.
- CI storage and dedupe pressure are fundamentally SHA-scoped, so PR-first enqueueing created unnecessary task fanout.
- We needed to reduce duplicate queue pressure while preserving PR-level analyzer refresh behavior.

## Decision
- Check-event webhooks now always enqueue SHA-first CI sync work via `syncer.sync_ci_for_repo_shas`.
- The webhook path no longer has a PR-fanout routing branch or feature flag for this decision.
- `syncer.sync_ci_for_repo_shas` performs CI-by-SHA ingest, then resolves impacted PRs and fans out analyzer follow-up (`analyzer.process_pr`) per impacted PR id.
- Impacted PR resolution uses:
  - `analyzer.PRRevision.head_sha` for historical head-SHA associations, and
  - `syncer.PullRequest.head_sha` fallback for recently-updated open PR heads not yet reflected in revision history.
- `syncer.sync_ci_for_shas` remains available for existing PR-scoped producers, but check-event routing is SHA-first by default architecture.

## Consequences
- Benefits:
  - lower enqueue multiplicity for check-event deliveries,
  - better alignment between task identity and CI data identity (`repo + sha`),
  - clearer observability on webhook deliveries vs enqueue outcomes vs analyzer fanout.
- Trade-offs:
  - analyzer fanout is still per impacted PR (not batched),
  - impacted-PR lookup cost depends on `PRRevision` query performance.
- Invariants:
  - CI correctness remains anchored by idempotent SHA-keyed upsert behavior,
  - dedupe/Redis failures remain fail-open for correctness.

## Operational Notes
- Implemented and deployed:
  - webhook check-event SHA-first routing,
  - impacted PR follow-up fanout from SHA task,
  - metrics fields:
    - `webhook_check_deliveries`,
    - `webhook_sha_first_tasks_enqueued`,
    - `sha_task_impacted_pr_fanout_total`.
- Observed production behavior after rollout:
  - high dedupe ratio for check deliveries,
  - low enqueue-per-delivery rate consistent with intended SHA-first dedupe effect.

### Deferred follow-up
- `PRRevision` head-SHA lookup currently performs a seq scan in sampled production-like data (~168k rows, ~26ms for no-hit sample).
- We are explicitly deferring index addition for now.
- Candidate index to evaluate later: `analyzer_prrevision(head_sha, pull_request_id)` (or equivalent Django model index).
- Revisit if `syncer.sync_ci_for_repo_shas` runtime or queue latency regresses.

## Alternatives
- Keep check-event routing PR-first with dedupe only:
  - rejected; still couples enqueue fanout to PR association shape.
- Introduce a new SHA↔PR mapping table:
  - not required for current scope because `PRRevision` already provides historical head-SHA association.
- Batch analyzer processing immediately:
  - deferred; per-PR fanout is simpler and sufficient at current scale.

## References
- `docs/design-decisions/030-sync-task-dedupe-strategy.md`
- `qb_site/syncer/views.py`
- `qb_site/syncer/tasks/sync_tasks.py`
- `qb_site/syncer/tasks/metrics_tasks.py`
- `qb_site/syncer/models/metrics.py`
