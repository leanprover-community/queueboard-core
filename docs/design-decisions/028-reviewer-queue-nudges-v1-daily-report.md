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
  - flag newly assigned PRs within configurable recent window,
  - send one summary DM per reviewer when action is needed,
  - unassign when `days_on_queue_since_assignment >= Y`.
- Add reviewer notification settings with:
  - `notifications_enabled` (boolean, default `False`)
  - configurable `X` and `Y` thresholds (initially in JSON settings to allow future options).
- Set default thresholds to:
  - `stale_nudge_days = 14`
  - `auto_unassign_days = 21`
- Enforce a hard maximum:
  - `auto_unassign_days <= 21`.
- Apply enforcement policy independently of notification preference:
  - `notifications_enabled` controls messaging behavior,
  - auto-unassign eligibility still applies at the effective enforcement threshold.
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
    - assigned within new-assignment window: needs new-assignment ping,
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
- `Y` is hard-capped at `21`.
- Auto-unassign at first run where `days >= Y`.
- Do not repeatedly unassign; treat as idempotent action by checking current assignees and prior recorded success.

### F) Notification toggle vs enforcement
- `notifications_enabled=False` suppresses reviewer nudge/report messaging.
- It does **not** exempt the reviewer from stale auto-unassign policy.
- Rationale: queue health policy should not depend on whether a reviewer opted into messaging.

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
#### Chunk A1: Notification preference schema + parsing defaults (**completed**)
1. Add `ReviewerPreference` fields:
  - `notifications_enabled` (default `False`)
  - `notification_settings` (JSON, default `{}`)
2. Add migration for those fields.
3. Add pure settings parser/normalizer (`X`, `Y`) with non-DB tests.
4. Keep existing preference forms unchanged in this chunk (no UX changes yet).

#### Chunk A2: Preference UI/admin wiring + validation
1. Add form fields for notifications in Zulip prefs form.
2. Add admin exposure for new notification controls.
3. Validate and normalize `notification_settings` values (`X >= 1`, `Y > X`) in form/service path.
4. Add form tests for valid/invalid submissions and persistence.

#### Chunk A3: Run-state persistence models (**deferred**)
1. Add minimal model(s) for run metadata and per-day dedupe records.
2. Add model indexes/constraints for idempotency keys.
3. Add tests for dedupe semantics and retry-safe writes.

#### Chunk A4: Policy computation service (read-only) (**completed**)
1. Implement DB-backed policy evaluator (no sends/mutations).
2. Compute per-reviewer report rows and unassign candidates from queue/timeline state.
3. Add unit tests for queue continuity, reassignment reset, and missing-data fallbacks.

### Sub-plan B: Daily report task (dry-run first)
#### Chunk B1: Task wiring and schedule (**completed**)
1. Add Celery task and beat schedule entry (daily).
2. Add feature flags for global enable + enforcement toggle.

#### Chunk B2: Dry-run execution path (**completed**)
1. Run policy evaluator and emit run summary to logs/metrics only.
2. Do not call Zulip or GitHub yet.
3. Add structured logs + admin visibility.

#### Chunk B3: Dry-run validation period
1. Run for several days.
2. Review "would-nudge/would-unassign" outputs and tune defaults.

### Sub-plan C: Enable messaging and enforcement
#### Chunk C1: Zulip summary delivery (**completed**)
1. Send one summary DM per reviewer when report non-empty.
2. Record delivery outcomes and retry-safe status.

#### Chunk C2: Auto-unassign execution
1. Enable GitHub unassign behind feature flag.
2. Execute idempotently and append results to summary.
3. Enforce hard max policy (`21`) regardless of reviewer notification toggle.
4. Add tests around duplicate runs and partial failures.

#### Chunk C3: Incremental rollout
1. Start with small cohort.
2. Expand after error rates and outcomes are acceptable.

### Sub-plan D: Stabilization and tuning
#### Chunk D1: Policy and UX tuning
1. Tune defaults for `X` and `Y`.
2. Improve report formatting and actionable links.

#### Chunk D2: Observability hardening
1. Add metrics/alerts for:
  - report generation failures,
  - Zulip delivery failures,
  - GitHub unassign failures,
  - skipped decisions due to missing data.

### Sub-plan E: Later migration of assignment producer
#### Chunk E1: Move assignment execution to `qb_site`
1. Implement native assignment executor task in Django.
2. Keep nudge policy/enforcement unchanged.

#### Chunk E2: Parity and retirement
1. Run parity/shadow period.
2. Remove/retire Action assignment step.

## Progress and Implementation Notes
- **Completed:** Chunk A1.
  - Added `notifications_enabled` and `notification_settings` to `core.ReviewerPreference`.
  - Added migration `core.0005_reviewerpreference_notifications`.
  - Added `core.services.reviewer_notification_settings.parse_notification_policy(...)` and tests.
- **Completed:** Chunk A2.
  - Added reviewer-facing notification controls in Zulip prefs form:
    - `notifications_enabled`
    - `stale_nudge_days` (X)
    - `auto_unassign_days` (Y)
  - Added form-layer validation for `Y > X` and defaulting behavior for blank values.
  - Wired persistence so form submissions store normalized values in `notification_settings`.
  - Exposed `notifications_enabled` in `ReviewerPreferenceAdmin` list display/filter.
- **Adjustment after A2:**
  - Updated defaults to `X=14`, `Y=21`.
  - Added hard max validation/cap for `Y<=21`.
  - Confirmed intended policy: notification opt-out does not disable stale auto-unassign enforcement.
- **Deferred:** Chunk A3.
  - We are intentionally shipping V1 without run-state/dedupe tables.
  - Near-term tradeoff is lower reliability under retries/overlapping runs and weaker post-hoc observability.
  - We keep A3 as a future hardening step if duplicate/missed notification behavior becomes operationally problematic.
- **Completed:** Chunk A4.
  - Added read-only policy service `build_reviewer_attention_reports(...)` that returns:
    - full per-reviewer status rows for on-demand reporting,
    - derived event flags (`needs_nudge`, `needs_auto_unassign`) for scheduled notification/enforcement.
    - queue duration rollups per assigned PR:
      - consecutive days since assignment anchor,
      - total queue time/days across queue windows (active ruleset scope).
  - Added service tests covering:
    - `X <= days < Y` nudge behavior,
    - `days >= Y` auto-unassign behavior,
    - enforcement flags still computed when notifications are disabled,
    - missing assignment timestamp fallback behavior,
    - queue re-entry reset behavior via active queue-window anchoring.
  - Added an on-demand consumer command: `assigned_prs` (Zulip private command) that renders reviewer-facing status summaries using A4 output.
- **Nuance discovered during implementation:**
  - Existing field-coverage guard (`reviewer_preference_unaccounted_fields`) requires every model field to be explicitly classified.
  - To keep this first chunk isolated and testable, new fields were intentionally added to `REVIEWER_PREFERENCE_NON_FORM_FIELDS` first, deferring UI exposure to Chunk A2.
  - Form submissions now write canonical threshold values into `notification_settings`; this means legacy rows with empty settings become explicit after first save.
  - A4 currently anchors queue-age at `max(last_assigned_at, active_queue_window.from_ts)`, which naturally resets stale age on queue re-entry even without persisting extra run-state.
- **2026-02-27 review chunk completed:**
  - Re-reviewed this living plan against current `qb_site/` implementation and relevant `AGENTS.md` guidance.
  - Confirmed A1/A2/A4 status is accurate and no immediate plan-structure changes are required.
  - Confirmed next execution target remains Sub-plan B / Chunk B1 (Celery task + beat wiring + feature flags).
- **Completed:** Chunk B1 + B2.
  - Added new Celery task `analyzer.reviewer_attention_daily` (`qb_site/analyzer/tasks/reviewer_attention.py`).
  - Added settings/feature flags:
    - `ANALYZER_REVIEWER_ATTENTION_ENABLED`
    - `ANALYZER_REVIEWER_ATTENTION_ENFORCEMENT_ENABLED`
    - `ANALYZER_REVIEWER_ATTENTION_PERIOD_SECONDS`
  - Added beat schedule wiring for `reviewer_attention_daily`.
  - Implemented dry-run execution that computes per-repo and total counts from `build_reviewer_attention_reports(...)` and logs run summaries.
  - Confirmed task is read-only for now (no Zulip sends, no GitHub unassign mutations).
  - Added task tests for feature-disabled behavior, dry-run summary aggregation, and repo-filter skip behavior.
- **2026-02-27 scheduling adjustment:**
  - Added optional fixed UTC daily clock scheduling for reviewer-attention beat:
    - `ANALYZER_REVIEWER_ATTENTION_UTC_HOUR`
    - `ANALYZER_REVIEWER_ATTENTION_UTC_MINUTE`
  - If either UTC clock setting is present, it overrides interval schedule (`ANALYZER_REVIEWER_ATTENTION_PERIOD_SECONDS`).
- **2026-02-27 policy adjustment (new-assignment trigger):**
  - Added a third notification trigger: newly assigned PRs within a configurable window.
  - Implemented as an additional flag in policy output (`needs_new_assignment_ping`) reusing existing assignment timeline data (no extra per-PR query path).
- **2026-02-27 window-derivation adjustment:**
  - Removed separate "new assignment window" setting.
  - The newly-assigned window is now derived from reviewer-attention sweep scheduling:
    - fixed UTC clock mode (`ANALYZER_REVIEWER_ATTENTION_UTC_HOUR` / `..._MINUTE`) => 24h window,
    - interval mode (`ANALYZER_REVIEWER_ATTENTION_PERIOD_SECONDS`) => interval-sized window.
- **Completed:** Chunk C1.
  - Added optional Zulip summary delivery in `analyzer.reviewer_attention_daily` behind `ANALYZER_REVIEWER_ATTENTION_DELIVERY_ENABLED`.
  - Sends one DM per reviewer per run (aggregated across repositories) when reviewer has events of interest and notifications enabled.
  - Message includes category-grouped PR lists:
    - newly assigned,
    - needs nudge,
    - auto-unassign candidates.
  - Added structured delivery outcomes in task result payload (attempted/sent/failed/skipped plus per-reviewer statuses).
  - Current retry-safety/observability note: outcomes are recorded in task result/logs, but no dedicated run-state tables yet (A3 remains deferred).
- **2026-02-27 message/UX tuning:**
  - Refined reviewer DM content for compact actionable summaries:
    - "Newly assigned" now includes assignment timestamp (`since <time:...>`) and relative age.
    - "Needs nudge" copy updated to compactly state consecutive queue days since assignment.
    - Auto-unassign section wording now distinguishes threshold vs actual unassignment based on enforcement mode.
  - Added actionable reminders in DM footer:
    - `unassign` command syntax example,
    - `prefs` command hint for notification setting changes.
  - Factored shared formatting/sorting helpers for reviewer attention views so daily DM and `assigned_prs` stay aligned on ordering and "assigned X ago" rendering.

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
  - without run-state persistence in V1, retries/overlapping runs can produce duplicate or missing daily notifications.

## Alternatives Considered
- Build notifications directly in GitHub Actions.
  - Rejected: poor fit for DB-backed policy logic, weak retry/audit ergonomics, duplicates Django logic.
- Build full event schema/event bus first.
  - Deferred: strong long-term architecture, but too much initial complexity for V1 goals.
- Trigger immediate reassignment when unassigning at `Y`.
  - Deferred: creates coupling with assignment producer while assignment still runs outside Django.

## Open Questions
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
