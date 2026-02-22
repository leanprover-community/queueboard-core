# Reviewer Queue Nudges V1 (Daily Report + Auto-Unassign)

## Context
- Goal: nudge reviewers to move assigned PRs through the queue in a timely way, and automatically unassign when stale long enough.
- Current assignment execution still runs in GitHub Actions:
  - `.github/workflows/auto_assign_reviewers.yaml`
  - `scripts/assign_reviewers.py`
- Queueboard already has the state needed for queue-aware policy decisions in Django:
  - reviewer preferences (`core.ReviewerPreference`)
  - PR assignment timeline (`syncer.PRTimelineEvent`)
  - queue windows (`analyzer.PRQueueWindow`)
  - Zulip messaging client (`zulip_bot.services.zulip_client`)
  - GitHub assignment mutation client (`core.services.github_assignment`)
- We considered an event-bus-first architecture, but for V1 this is likely unnecessary scope.
- Requirement for this phase: ship practical nudges/unassignment with low churn now, while preserving a clean path to later move assignment execution into `qb_site`.

## Decision
- Implement a **daily reviewer attention report + auto-unassign task in `qb_site`**.
- Keep assignment execution in GitHub Actions for now.
- Do **not** introduce a generic event bus/event schema for V1.
- Model V1 as a policy sweep:
  - iterate reviewers with notifications enabled,
  - inspect currently assigned PRs,
  - compute queue age since the reviewer's most recent assignment to each PR,
  - send one summary DM per reviewer when action is needed,
  - unassign when `days_on_queue_since_assignment >= Y`.
- Add reviewer notification settings with:
  - `notifications_enabled` (boolean, default `False`)
  - configurable `X` and `Y` thresholds (initially in JSON settings to allow future options).
- Preserve a seam for migration:
  - keep policy/enforcement logic independent from assignment producer,
  - later switch producer from GitHub Action to `qb_site` assignment task without redesigning nudges.

## V1 Architecture

### 1) Scheduler and execution boundary
- Add a Celery beat task in `qb_site` that runs daily after the assignment workflow and ingestion lag buffer.
- Task responsibilities:
  - evaluate attention conditions,
  - execute unassignments,
  - send per-reviewer summary DMs,
  - persist enough run state for dedupe and observability.

### 2) Inputs
- Reviewer configuration from `core.ReviewerPreference`.
- Current open PR assignment state from `syncer.PullRequest.assignees`.
- Assignment history from `syncer.PRTimelineEvent` (`ASSIGNED` / `UNASSIGNED`).
- Queue membership/continuity from `analyzer.PRQueueWindow` under the active ruleset.

### 3) Policy engine (daily sweep)
- For each reviewer with notifications enabled:
  - collect open PRs currently assigned to that reviewer,
  - for each PR, find the reviewer’s **most recent assignment timestamp**,
  - compute consecutive queue duration since that assignment,
  - classify:
    - `>= X` and `< Y`: needs nudge,
    - `>= Y`: auto-unassign candidate.
- Build one summary payload per reviewer (single DM per run, only when non-empty).

### 4) Enforcement and delivery
- Unassign via `GitHubAssignmentClient.unassign(...)` before finalizing summary content.
- Send summary via Zulip direct message.
- Keep send + mutation outcomes in DB so retries and idempotency are safe.

### 5) Persistence (minimal V1 state)
- Add a compact run-state model (or models) for:
  - dedupe of repeated notifications for the same `(repo, pr, reviewer, day, category)`,
  - tracking attempted/succeeded auto-unassign operations,
  - run metadata (`started_at`, `completed_at`, counts, errors).
- Keep storage narrow and purpose-built; do not generalize into an all-events ledger in V1.

## Policy Semantics and Subtleties

### A) "Consecutive days on queue since most recent assignment"
- Anchor is the reviewer-specific latest `ASSIGNED` event for that PR.
- Count queue time only from `max(last_assigned_at, first_on_queue_after_that_assignment)`.
- If PR leaves queue and later re-enters, continuity resets based on queue windows.
- If reviewer is reassigned later, clock resets to the new assignment timestamp.

### B) Missing or delayed data
- If assignment timestamp for a currently assigned reviewer cannot be determined confidently:
  - skip auto-unassign for that PR in this run,
  - include optional diagnostic metric/log entry.
- If queue windows are stale or unavailable for the active ruleset:
  - skip strict actions for affected PRs (fail safe),
  - surface run warning and convergence metric.

### C) Unassign threshold behavior
- `Y` must be strictly greater than `X`.
- Auto-unassign at first run where `days >= Y`.
- Do not repeatedly unassign; treat as idempotent action by checking current assignees and prior recorded success.

### D) Reassignment behavior after auto-unassign
- V1: no immediate reassignment inside this task.
- Reassignment remains the responsibility of the existing assignment workflow on its next cycle.
- Rationale: avoids coupling daily nudge task to assignment execution while assignment still lives in GitHub Actions.

### E) Notification mode
- V1 supports summary messaging only (one DM/day/reviewer as needed).
- Live per-event pings are deferred.

## Data Model and Settings Plan

### 1) Reviewer preferences
- Extend `core.ReviewerPreference`:
  - `notifications_enabled = models.BooleanField(default=False)`
  - `notification_settings = models.JSONField(default=dict, blank=True)`
- Initial JSON keys:
  - `stale_nudge_days` (X)
  - `auto_unassign_days` (Y)
  - optional future keys (quiet hours, channel mode, etc.)

### 2) Validation rules
- `X >= 1`
- `Y >= 2`
- `Y > X`
- enforce at form/service layer; store normalized ints in JSON.

### 3) Operational settings
- Add task schedule config in `qb_site/settings/base.py`:
  - run period (daily),
  - optional UTC hour/minute window if later moved from fixed-seconds schedule.
- Add feature flags:
  - global enable for report generation,
  - global enable for auto-unassign enforcement (supports dry-run).

## Implementation Plan

### Sub-plan A: V1 foundation (data + policy skeleton)
1. Add `ReviewerPreference` notification fields and migration.
2. Add/update forms/admin/prefs page to expose the new controls.
3. Add a policy service module in `analyzer` (or `core`) that computes reviewer attention items from DB state.
4. Add unit tests for threshold validation and queue-age computation edge cases.

### Sub-plan B: Daily report task (dry-run first)
1. Add Celery task and beat schedule entry.
2. Implement dry-run mode:
  - compute report rows,
  - persist run summary,
  - do not mutate GitHub or send Zulip.
3. Add logging and basic admin visibility for dry-run output.
4. Validate on production-like data for at least several days.

### Sub-plan C: Enable messaging and enforcement
1. Enable Zulip summary DM sending.
2. Add unassign execution path behind feature flag.
3. Add idempotency checks and retry handling.
4. Roll out gradually (small cohort, then full).

### Sub-plan D: Stabilization and tuning
1. Tune defaults for `X` and `Y`.
2. Improve report formatting and actionable links.
3. Add metrics/alerts for:
  - report generation failures,
  - Zulip delivery failures,
  - GitHub unassign failures,
  - skipped decisions due to missing data.

### Sub-plan E: Later migration of assignment producer
1. Move assignment execution from GitHub Actions into `qb_site` task.
2. Keep nudge policy/enforcement unchanged.
3. Remove/retire Action assignment step after parity period.

## Operational Notes
- Suggested schedule relationship:
  - assignment run (GitHub Action) first,
  - then sync ingestion,
  - then daily nudge task after a buffer to reduce stale-read risk.
- For initial rollout:
  - run with `auto-unassign` disabled,
  - compare "would unassign" outputs against maintainer expectations,
  - then enable enforcement.
- Keep runbook entries for:
  - reverting an unintended unassignment,
  - temporarily disabling enforcement globally,
  - replaying one reviewer/day report generation.

## Consequences
- Pros:
  - ships core reviewer nudge value quickly,
  - uses existing Django data and infrastructure,
  - avoids heavy event-system upfront complexity,
  - preserves a clean migration path for assignment execution later.
- Trade-offs:
  - daily summaries are less immediate than live pings,
  - correctness depends on freshness of timeline/queue data at run time,
  - some minimal run-state persistence is still needed for safe idempotency.

## Alternatives Considered
- Build notifications directly in GitHub Actions.
  - Rejected: poor fit for DB-backed policy logic, weak retry/audit ergonomics, duplicates Django logic.
- Build full event schema/event bus first.
  - Deferred: strong long-term architecture, but too much initial complexity for V1 goals.
- Trigger immediate reassignment when unassigning at `Y`.
  - Deferred: creates coupling with assignment producer while assignment still runs outside Django.

## Open Questions
- Defaults for `X` and `Y` at rollout.
- Whether to include "newly assigned today" in summary when assignment provenance is ambiguous.
- Whether per-repository overrides are needed in `notification_settings` or global per reviewer is sufficient.

## References
- `.github/workflows/auto_assign_reviewers.yaml`
- `scripts/assign_reviewers.py`
- `qb_site/core/models/reviewer_preference.py`
- `qb_site/syncer/models/pull_request.py`
- `qb_site/syncer/models/pr_timeline_event.py`
- `qb_site/analyzer/models/queue_window.py`
- `qb_site/core/services/github_assignment.py`
- `qb_site/zulip_bot/services/zulip_client.py`
