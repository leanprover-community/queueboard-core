# Reviewer Queue Nudges and Auto-Unassign (Daily Attention Sweep)

## Context
- Queueboard needs queue-health enforcement for assigned reviewers without coupling to immediate assignment production changes.
- Assignment production still runs in GitHub Actions (`.github/workflows/auto_assign_reviewers.yaml`, `scripts/assign_reviewers.py`).
- Django already has the data and integrations required for policy evaluation and enforcement:
  - reviewer preferences (`core.ReviewerPreference`),
  - assignment timeline (`syncer.PRTimelineEvent`),
  - queue continuity windows (`analyzer.PRQueueWindow`),
  - Zulip direct messaging (`zulip_bot.services.zulip_client`),
  - GitHub unassign mutation client (`core.services.github_assignment`).
- Requirements for this phase were:
  - enforce stale-assignment policy,
  - send actionable reviewer summaries,
  - keep assignment producer migration optional for later,
  - make retries/idempotency operationally safe.

## Decision
- Implement reviewer attention as a Django/Celery policy sweep (`analyzer.reviewer_attention_daily`) that:
  - computes per-reviewer queue attention state,
  - optionally sends one aggregated Zulip DM per reviewer,
  - optionally executes GitHub auto-unassign mutations,
  - persists run-state and dedupe data for retry/idempotency safety.
- Keep assignment production outside Django for now.
- Use reviewer-specific threshold policy:
  - `stale_nudge_days` (X),
  - `auto_unassign_days` (Y),
  - defaults `X=14`, `Y=21`, hard max `Y<=21`, and `Y>X`.
- Enforce stale auto-unassign independently of notification opt-in:
  - `notifications_enabled` gates DM delivery,
  - enforcement still applies when enabled globally.
- Use per-cycle dedupe for notifications (not per-day):
  - one notification per `(repo, reviewer, pr, category, cycle_anchor_at)`.

## Architecture

### 1) Policy Evaluation Service
- Service: `analyzer.services.reviewer_attention.build_reviewer_attention_reports(...)`.
- Inputs:
  - repository,
  - current open PR assignees,
  - latest reviewer assignment event per PR,
  - active queue window under active ruleset,
  - reviewer threshold policy.
- Outputs:
  - per-reviewer `ReviewerAttentionReport` with `ReviewerAttentionItem` rows,
  - event flags per item:
    - `needs_new_assignment_ping`,
    - `needs_nudge`,
    - `needs_auto_unassign`.

### 2) Queue-Age and Reset Semantics
- Consecutive queue age anchor:
  - `queue_anchor_at = max(last_assigned_at, active_queue_window.from_ts)`.
- Effects:
  - reassignment resets age,
  - queue re-entry resets age,
  - missing assignment timestamp suppresses strict actions and emits warning context.
- Optional policy floor:
  - `ANALYZER_REVIEWER_ATTENTION_POLICY_START_AT` clamps counting anchor forward.

### 3) Daily Task Orchestration
- Task: `analyzer.reviewer_attention_daily`.
- Sequence:
  1. resolve effective runtime toggles (global flags + optional per-run overrides),
  2. evaluate policy reports by repository,
  3. execute optional auto-unassign mutations,
  4. execute optional reviewer DM delivery,
  5. persist run summary and outcomes.
- Delivery model:
  - one DM per reviewer per run when at least one claimable event exists,
  - message sections grouped by event category and repository.

### 4) Enforcement Path
- Auto-unassign candidates come from `needs_auto_unassign` flags.
- Mutation path:
  - resolve operation token (`unassign_pr`),
  - call `GitHubAssignmentClient.unassign(...)`.
- Enforcement idempotency:
  - de-duped by persisted `(run_date, repository, reviewer, pr_number)` key.

### 5) Run-State and Dedupe Persistence
- Models (`analyzer.models.reviewer_attention_run_state`):
  - `ReviewerAttentionDailyRun`: task run metadata and summary payload,
  - `ReviewerAttentionNotificationRecord`: notification dedupe and delivery outcomes,
  - `ReviewerAttentionAutoUnassignRecord`: enforcement outcomes.
- Notification dedupe key:
  - `(repository, reviewer, pr_number, category, cycle_anchor_at)`.
- Category-scoped behavior is intentional:
  - a PR can emit one `new_assignment` and later one `nudge` in same cycle.
- Retry behavior:
  - failed notification records are claimable on later runs,
  - stale `pending` notification records are reclaimable after timeout to avoid deadlock.

### 6) Cleanup Lifecycle
- Task: `analyzer.reviewer_attention_cleanup`.
- Cleanup policy:
  - notification records: delete only when older than retention and either:
    - PR is no longer open, or
    - reviewer is no longer assigned.
  - auto-unassign records: age-based retention deletion,
  - run metadata: age-based retention deletion.
- Rationale:
  - preserve dedupe guarantees for still-open and still-assigned cycles.

### 7) Admin and Operational Controls
- Manual run tooling in `core.ReviewerPreference` admin supports targeted/manual runs.
- Read-only analyzer admin visibility exists for:
  - `ReviewerAttentionDailyRun`,
  - `ReviewerAttentionNotificationRecord`,
  - `ReviewerAttentionAutoUnassignRecord`.

## Configuration and Scheduling

### Feature Flags
- `ANALYZER_REVIEWER_ATTENTION_ENABLED`
- `ANALYZER_REVIEWER_ATTENTION_ENFORCEMENT_ENABLED`
- `ANALYZER_REVIEWER_ATTENTION_DELIVERY_ENABLED`

### Daily Sweep Schedule
- Interval mode:
  - `ANALYZER_REVIEWER_ATTENTION_PERIOD_SECONDS`
- UTC clock mode (overrides interval when present):
  - `ANALYZER_REVIEWER_ATTENTION_UTC_HOUR`
  - `ANALYZER_REVIEWER_ATTENTION_UTC_MINUTE`

### New-Assignment Window Derivation
- Derived from sweep scheduling mode:
  - fixed UTC clock mode => 24h window,
  - interval mode => interval-sized window.

### Cleanup Schedule (Crontab)
- `ANALYZER_REVIEWER_ATTENTION_CLEANUP_DAY_OF_WEEK` (default `sun`)
- `ANALYZER_REVIEWER_ATTENTION_CLEANUP_UTC_HOUR` (default `3`)
- `ANALYZER_REVIEWER_ATTENTION_CLEANUP_UTC_MINUTE` (default `0`)

### Cleanup Retention
- `ANALYZER_REVIEWER_ATTENTION_NOTIFICATION_RETENTION_DAYS` (default `30`)
- `ANALYZER_REVIEWER_ATTENTION_AUTO_UNASSIGN_RETENTION_DAYS` (default `90`)
- `ANALYZER_REVIEWER_ATTENTION_RUN_RETENTION_DAYS` (default `30`)

## Operational Notes
- Recommended execution order for freshness:
  1. assignment producer run,
  2. sync ingestion,
  3. reviewer attention sweep after buffer.
- Rollout sequence:
  1. reports enabled, delivery off, enforcement off,
  2. delivery on,
  3. enforcement on.
- Enforcement and messaging can be independently toggled globally and per manual run.
- On Heroku/ephemeral filesystems, cron-style schedules are preferred over long fixed intervals for reliability across restarts.

## Testing Strategy
- Service tests (`analyzer/tests/services/test_reviewer_attention.py`) cover:
  - threshold transitions,
  - queue re-entry reset,
  - missing assignment fallback,
  - policy-floor behavior.
- Task tests (`analyzer/tests/tasks/test_reviewer_attention_task.py`) cover:
  - feature toggles,
  - delivery and enforcement paths,
  - partial failures,
  - dedupe and retry semantics,
  - reassignment/queue-reentry/settings-change category transitions.
- Cleanup tests (`analyzer/tests/tasks/test_reviewer_attention_cleanup_task.py`) cover:
  - safe deletion for closed/unassigned cases,
  - retention pruning for run and enforcement records,
  - preservation of still-needed dedupe rows.

## Consequences
- Pros:
  - queue-health nudges and enforcement shipped without migrating assignment producer,
  - robust retry/idempotency behavior with explicit run-state,
  - operational visibility and manual controls in admin,
  - cleanup lifecycle avoids unbounded growth while preserving active-cycle dedupe.
- Trade-offs:
  - policy quality depends on sync and queue-window freshness,
  - per-cycle dedupe still requires retained rows for open+assigned PRs,
  - daily sweep is less immediate than event-driven notifications.

## Alternatives Considered
- Implement nudges/enforcement in GitHub Actions.
  - Rejected: poorer DB-backed policy evaluation and operational observability.
- Introduce generalized event bus/event ledger first.
  - Rejected for this phase: unnecessary scope before delivering policy value.
- Couple unassign task to immediate reassignment.
  - Deferred: intentionally kept assignment production decoupled.

## Open Questions
- Whether to add per-repository threshold overrides in `notification_settings` beyond current per-reviewer global thresholds.

## References
- `.github/workflows/auto_assign_reviewers.yaml`
- `scripts/assign_reviewers.py`
- `qb_site/core/models/reviewer_preference.py`
- `qb_site/core/services/reviewer_notification_settings.py`
- `qb_site/core/services/github_assignment.py`
- `qb_site/analyzer/services/reviewer_attention.py`
- `qb_site/analyzer/services/reviewer_attention_format.py`
- `qb_site/analyzer/tasks/reviewer_attention.py`
- `qb_site/analyzer/tasks/reviewer_attention_cleanup.py`
- `qb_site/analyzer/models/reviewer_attention_run_state.py`
- `qb_site/analyzer/admin.py`
- `qb_site/syncer/models/pull_request.py`
- `qb_site/syncer/models/pr_timeline_event.py`
- `qb_site/analyzer/models/queue_window.py`
- `qb_site/zulip_bot/services/zulip_client.py`
