# Apply Reviewer Assignments From Django (Retire GitHub Actions Auto-Assign)

## Context
- Reviewer auto-assignment for `leanprover-community/mathlib4` is currently applied by a
  GitHub Actions workflow, `.github/workflows/auto_assign_reviewers.yaml` (daily at 00:37 UTC),
  which runs `scripts/assign_reviewers.py`. That script:
  - downloads `automatic_assignments.json` from the **legacy** queueboard GitHub Pages site
    (`https://leanprover-community.github.io/queueboard/automatic_assignments.json`), produced by
    `src/queueboard/dashboard_data.py` via `src/queueboard/suggest_reviewer.py`, and
  - `POST`s each `{pr_number: reviewer_login}` pair to
    `/repos/leanprover-community/mathlib4/issues/{number}/assignees` with a token minted from the
    `MATHLIB_TRIAGE_APP` GitHub App.
- The **compute** half already lives in Django. `analyzer.refresh_reviewer_assignments` (daily) builds
  `ReviewerAssignmentSnapshot.payload["automatic_assignments"]` (= `{pr_number: reviewer_login}`),
  fanning out **one snapshot per active `QueueRuleSet`** (cache key = rule-set id, or `"default"`; see
  `039-queue-ruleset-default-designation-and-snapshot-cache-keys.md`). This is the "computing possible
  assignments for each active queue rule set" job.
- The **mutation** plumbing also already exists and is in production use:
  - `core.services.github_assignment.GitHubAssignmentClient.assign_many()` /
    `unassign_many()` (`026-zulip-assign-unassign-and-github-app-tokens.md`),
  - the `assign_pr` / `unassign_pr` operation tokens via the `queueboard-assignment` GitHub App
    (`027-github-app-operation-token-services.md`),
  - already consumed by the Zulip `/assign` command and by the auto-**unassign** path in
    `analyzer.reviewer_attention_daily`.
- `028-reviewer-queue-nudges-v1-daily-report.md` explicitly deferred this work:
  *"Keep assignment production outside Django for now."* This document is that deferred follow-up.

### What is actually missing
1. A Django job that reads the freshly computed snapshot and **applies** it to GitHub (the equivalent
   of `scripts/assign_reviewers.py`, but sourcing the Django snapshot instead of the legacy JSON).
2. **Idempotency / audit tracking.** There is no record that "this proposed assignment was applied",
   so nothing prevents re-posting the same assignment on every daily run, nor clobbering a PR whose
   assignee a human (or the attention sweep) deliberately removed.

## Goals / Non-Goals
- Goals:
  - Move assignment **application** into a scheduled Celery task in `qb_site/analyzer/`.
  - Source assignments from the Django `ReviewerAssignmentSnapshot` (default rule set), not the legacy
    GitHub Pages JSON.
  - Track applied assignments for idempotency and operator audit.
  - Retire `.github/workflows/auto_assign_reviewers.yaml` and `scripts/assign_reviewers.py`.
- Non-Goals:
  - Changing the assignment **algorithm** or snapshot schema (`037`, `039` are unchanged).
  - Changing reviewer-attention notifications / auto-unassign (`028`).
  - Retiring the legacy `dashboard_data.py` compute path or the static `automatic_assignments.json`
    (it may still feed the static site / API bridge — out of scope here).

## Decisions (confirmed)
- **Authoritative rule set:** apply **only the default rule set** snapshot per repo
  (`cache_key = "default"` / `QueueRuleSet.is_default`). Other active rule-set variants remain
  compute-only for the dashboard/API. Rationale: a PR can only carry one applied assignment; the
  default set mirrors the single legacy `automatic_assignments.json`.
- **Trigger model:** a **separate daily beat task** (`analyzer.apply_reviewer_assignments`) that reads
  the latest fresh default snapshot. Decoupled from the build task so apply can be retried/re-run
  without recomputing, and so a build failure does not entangle with a mutation failure.
- **Cutover:** **enable the task and delete the GitHub Actions workflow in the same change.** A kill
  switch (feature flag) and a dry-run mode are still provided for operability and a manual pre-merge
  sanity check, but there is no multi-day shadow period.

## Proposed Design

### Job pipeline (existing + new)
```
syncer.*                              ── ingest PRs, labels, CI, assignees      (~5m)
analyzer.refresh_queueboard_snapshots ── QueueSnapshot                          (~5m)
analyzer.refresh_reviewer_assignments ── ReviewerAssignmentSnapshot (COMPUTE)   (daily, per ruleset)
analyzer.apply_reviewer_assignments   ── POST assignees to GitHub (APPLY)       (daily)   ◀── NEW
analyzer.reviewer_attention_daily     ── notify + auto-UNASSIGN stale           (daily)
```
Recommended daily ordering: **compute → apply → attention** (assign early, then evaluate staleness
later in the day). Because apply re-validates against live data and is idempotent, exact ordering is
not load-bearing, but scheduling apply shortly after compute minimizes the staleness window. Both the
compute refresh (default **00:30 UTC**) and apply (default **00:45 UTC**) now run on a fixed daily
`crontab` rather than a rolling interval, so the 15-minute gap is deterministic; the times are
overridable via their `..._UTC_HOUR/MINUTE` settings.

### New task: `analyzer.apply_reviewer_assignments`
Mirrors the structure of the existing `reviewer_attention_daily` auto-unassign path
(`qb_site/analyzer/tasks/reviewer_attention.py`). Per eligible repo:
1. Load the latest **default-rule-set** `ReviewerAssignmentSnapshot`. Skip the repo if none exists or
   it is older than `ANALYZER_REVIEWER_ASSIGNMENT_APPLY_MAX_AGE_HOURS` (guards against acting on a
   stale snapshot when compute is broken).
2. For each `{pr_number: reviewer_login}` in `payload["automatic_assignments"]`, re-validate before
   mutating (the snapshot can be up to a day old):
   - **Already assigned?** Skip if the PR currently has any active assignee per the latest synced data
     / queue snapshot. This is the primary correctness guard against the staleness window.
   - **Recently applied?** Skip if an `applied` `ReviewerAssignmentApplication` exists for
     `(repo, pr_number, reviewer_login)` within
     `ANALYZER_REVIEWER_ASSIGNMENT_APPLY_DEDUPE_DAYS` (covers the sub-sync-cycle window where we
     POSTed minutes ago but `sync_pr` has not yet reflected it).
   - **Opted out?** Skip if an active `ReviewerOptOut` exists for `(repo, pr_number, reviewer_login)`
     (`020-reviewer-opt-outs-and-timeline-assignments.md`) — re-checked at apply time, not just at
     build time.
   - **Reviewer still eligible?** Skip if `ReviewerPreference.auto_assign` is false or `away_until` is
     in the future (defends against a preference change since the snapshot was built).
3. Resolve the `assign_pr` token via `resolve_github_app_operation_token(operation="assign_pr", ...)`.
   If no token, record `skipped_no_token` and continue.
4. If the feature flag is off (or dry-run), record `skipped_disabled` / `skipped_dry_run` with the
   intended action and continue (no mutation).
5. Call `GitHubAssignmentClient.assign(...)`, record the outcome, and enqueue `syncer.sync_pr` for the
   affected PR so the new assignee converges into our state (same post-mutation convergence pattern as
   the Zulip command and the auto-unassign path).
6. Return a concise summary dict (`attempted`, `assigned`, `failed`, `skipped_*` counters) like the
   other sweep tasks.

Repo eligibility = repos that have an active default rule set **and** at least one
`ReviewerPreference` with `auto_assign=True` (naturally scopes to mathlib4 today without hardcoding).

### New model: `analyzer.ReviewerAssignmentApplication`
Modeled on `ReviewerAttentionAutoUnassignRecord`. Fields:
- `repository` (FK → `core.Repository`)
- `pr_number` (int)
- `reviewer_login` (str)
- `snapshot` (FK → `ReviewerAssignmentSnapshot`, `on_delete=SET_NULL`, provenance)
- `run_date` (date) — groups a daily run
- `status` — one of `applied` / `failed` / `skipped_already_assigned` / `skipped_opted_out` /
  `skipped_ineligible` / `skipped_no_token` / `skipped_disabled` / `skipped_dry_run`
- `applied_at` (datetime, nullable)
- `error` (text, blank)
- `created_at` / `updated_at`
- Index on `(repository, pr_number, reviewer_login, status)` to make the dedupe lookup cheap.

### Settings / flags (add to BOTH `settings/base.py` and `.env.example`)
- `ANALYZER_REVIEWER_ASSIGNMENT_APPLY_ENABLED` (bool, default depends on env; kill switch)
- `ANALYZER_REVIEWER_ASSIGNMENT_APPLY_DRY_RUN` (bool, default false; log-only)
- `ANALYZER_REVIEWER_ASSIGNMENT_APPLY_UTC_HOUR` / `..._UTC_MINUTE` (daily UTC clock time;
  default 00:45) and `..._PERIOD_SECONDS` (set `<=0` to disable scheduling). The compute refresh
  gained matching `ANALYZER_REVIEWER_ASSIGNMENT_UTC_HOUR/MINUTE` knobs (default 00:30) so the two run
  in a deterministic order.
- `ANALYZER_REVIEWER_ASSIGNMENT_APPLY_MAX_AGE_HOURS` (default 48)
- `ANALYZER_REVIEWER_ASSIGNMENT_APPLY_DEDUPE_DAYS` (default 7)
- `ANALYZER_REVIEWER_ASSIGNMENT_APPLY_MAX_PER_REPO` (safety cap on mutations per run)
- Beat entry `apply_reviewer_assignments` in `CELERY_BEAT_SCHEDULE`.

### Idempotency semantics (important)
- This intentionally **allows re-cycling**: if a reviewer is later auto-unassigned (attention sweep) or
  removed and the PR becomes stale-unassigned again beyond the dedupe window, the builder may re-propose
  (excluding opted-out reviewers) and apply will pick it up again. This matches the legacy "stale
  unassigned" behavior rather than locking a PR forever after one assignment.
- The dedupe window + live-assignee re-check together prevent the failure mode of re-posting the same
  assignment daily during the brief window before `sync_pr` reflects the new assignee.

## Subtleties / Invariants
- Apply must read the **default** snapshot only; never apply non-default rule-set variants.
- All GitHub writes go through `GitHubAssignmentClient` + `assign_pr` operation token — never a raw PAT
  and never `curl`. No new auth path is introduced.
- Re-validate every assignment against live data at apply time; the snapshot is advisory, not a command.
- Every mutation enqueues `sync_pr` for convergence.
- Respect `ReviewerOptOut`, `ReviewerPreference.auto_assign`, and `away_until` at apply time.
- Honor the per-repo mutation cap and the feature flag; dry-run produces records but performs no writes.

## Implementation Plan (Chunks)
1. **Model + migration:** `ReviewerAssignmentApplication` in `qb_site/analyzer/models/`, generated
   migration (on host), `admin.py` registration (`ReadOnlyAdmin`, with `list_display` / `list_filter`
   covering status, repo, run_date), and backup-policy coverage (`scripts/backup_policy.py`).
2. **Service:** `apply_assignments_for_repo(repo, *, dry_run, now, ...)` in
   `qb_site/analyzer/services/` holding the load → re-validate → mutate → record logic (unit-testable
   without Celery).
3. **Task:** `analyzer.apply_reviewer_assignments` in `qb_site/analyzer/tasks/`, fanning out per
   eligible repo, returning a summary dict.
4. **Settings + beat:** add the env-backed settings to `settings/base.py` and `.env.example`; wire the
   beat schedule entry.
5. **Management command** (optional but recommended): `apply_reviewer_assignments --dry-run [--repo ...]`
   for the pre-merge sanity check and targeted recovery.
6. **Cutover:** delete `.github/workflows/auto_assign_reviewers.yaml` and `scripts/assign_reviewers.py`;
   confirm the `queueboard-assignment` GitHub App is installed on mathlib4 with assignment write access
   in production `GITHUB_APP_TOKEN_CONFIG` (the old workflow used `MATHLIB_TRIAGE_APP`).
7. **Docs:** update `qb_site/analyzer/AGENTS.md` task surface (and root mention) with the new task name;
   convert this living plan toward a final record once shipped.

## Validation Plan
- tests:
  - service unit tests: applies a proposal; skips already-assigned / opted-out / ineligible / recently
    applied; respects dry-run and the per-repo cap; records correct statuses; enqueues `sync_pr`.
  - task test: fan-out over eligible repos, summary dict shape, flag-off path.
- manual checks:
  - run the management command with `--dry-run` against mathlib4 and diff the would-assign set against
    a recent legacy `automatic_assignments.json` to confirm parity before enabling.
  - after enabling, verify `ReviewerAssignmentApplication` rows + a real assignment land, and that a
    second run is a no-op.
- Full validation via `bash scripts/repo_check_compose.sh` (includes backup-policy validation).

## Operational Notes
- Removing the workflow also removes the `workflow-keepalive` job; Celery beat does not get
  auto-disabled the way scheduled GitHub workflows do after repo inactivity.
- Kill switch: set `ANALYZER_REVIEWER_ASSIGNMENT_APPLY_ENABLED=0` to halt all auto-assignment writes
  without code changes.
- Prerequisite: `queueboard-assignment` app installation on mathlib4 must grant issue-assignee write.

## Alternatives (discarded)
- **Apply every active rule set's snapshot:** impossible — a PR can hold only one applied assignment.
- **Chain apply off each per-repo build:** tighter coupling; harder to retry apply independently; mixes
  compute and mutation failure domains. Rejected in favor of a standalone daily task.
- **Permanent per-PR assignment lock:** would prevent the desirable re-cycling of stale PRs after an
  auto-unassign. Rejected in favor of a recency-windowed dedupe + live re-check.

## Related Decisions
- `020-reviewer-opt-outs-and-timeline-assignments.md`
- `026-zulip-assign-unassign-and-github-app-tokens.md`
- `027-github-app-operation-token-services.md`
- `028-reviewer-queue-nudges-v1-daily-report.md` (deferred this work)
- `037-reviewer-assignment-policy-simulation-and-priority-planning.md`
- `039-queue-ruleset-default-designation-and-snapshot-cache-keys.md`

## Progress Notes
- 2026-06-21: Initial plan drafted. Decisions confirmed: default rule set only; standalone daily beat
  task; enable + delete workflow in the same change; design-decision doc authored before implementation.
- 2026-06-21: Implemented. Landed:
  - `analyzer.ReviewerAssignmentApplication` model + migration `0030`; admin (`ReadOnlyAdmin`) and
    backup-policy coverage (BACKUP + TRUNCATE).
  - `analyzer/services/reviewer_assignment_apply.py::apply_assignments_for_repo` — loads the default
    rule set's snapshot (`cache_key = str(default_rule_set.id)`, else `"default"`), re-validates each
    proposal (eligibility / opt-out / already-assigned-or-not-open / dedupe window), mutates via
    `GitHubAssignmentClient` (`assign_pr` token), records outcomes, enqueues `sync_pr`.
  - `analyzer.apply_reviewer_assignments` task (fan-out per eligible repo; no-op when neither enabled
    nor dry-run) + `apply_reviewer_assignments` beat entry (clock-or-period, like reviewer attention).
  - Settings `ANALYZER_REVIEWER_ASSIGNMENT_APPLY_*` in `base.py` + `.env.example` (default disabled).
  - Management command `apply_reviewer_assignments [--repo ...] [--dry-run] [--enable]`.
  - Service + task unit tests.
  - Cutover: deleted `.github/workflows/auto_assign_reviewers.yaml` and `scripts/assign_reviewers.py`;
    updated doc 028 references and the analyzer task surface.
- 2026-06-21: Scheduling pinned to fixed UTC clock times. Both `refresh_reviewer_assignments`
  (default 00:30 UTC) and `apply_reviewer_assignments` (default 00:45 UTC) now schedule via `crontab`
  instead of a rolling 24h interval, so compute runs deterministically ~15 min before apply. Times are
  overridable via `ANALYZER_REVIEWER_ASSIGNMENT_UTC_HOUR/MINUTE` and
  `ANALYZER_REVIEWER_ASSIGNMENT_APPLY_UTC_HOUR/MINUTE`; `..._PERIOD_SECONDS<=0` disables scheduling.
  Crontab is interpreted in `CELERY_TIMEZONE` (defaults to UTC). Also fixed a gating-order bug where
  dry-run runs (`enabled=False, dry_run=True`) recorded `skipped_disabled` instead of `skipped_dry_run`.
  - Validation: `ruff check`/`format` clean and `manage.py check` passes locally; DB-backed tests
    (`test analyzer`) and `scripts/repo_check_compose.sh` still need to run under Compose (Docker
    unavailable in the authoring sandbox). Production rollout sets
    `ANALYZER_REVIEWER_ASSIGNMENT_APPLY_ENABLED=1` and requires the `queueboard-assignment` app
    installation to grant issue-assignee write on mathlib4.
