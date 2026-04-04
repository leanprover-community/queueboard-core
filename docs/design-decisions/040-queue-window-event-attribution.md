# Queue Window Event Attribution (Living Plan)

## Context
- `QueueWindow` records the interval `[from_ts, to_ts)` during which a PR was on the queue
  under a given ruleset, but carries no information about *why* each transition happened.
- For offline analysis we want to know what caused each window to open and close — was it a
  label change, a CI result, a PR state change, etc.?
- Queue windows are already computed by replaying boundary events chronologically in
  `_queue_windows_with_rules` (`qb_site/analyzer/services/queue_windows.py`). Boundary sources are:
  - `TimelineEvent` (label add/remove, draft toggle, open/close, force-push)
  - `CommitCheckRun` / `CommitStatusContext` (CI completion/update)
  - Synthetic boundaries (PR creation, ruleset `effective_from`, revision changes)
- The expire task (`syncer.expire_stale_ci_for_repo`) deletes superseded check run rows
  (see `038-commit-ci-row-expiry-and-superseded-cleanup.md`), which intersects with FK safety.
  Doc 038 previously noted "there are no FKs pointing into `CommitCheckRun` or
  `CommitStatusContext` from other models" — this plan changes that.

## Goals / Non-Goals
- Goals:
  - Add `opened_by_*` and `closed_by_*` fields to `QueueWindow` recording the triggering event.
  - Attribute each transition to at most one source event (the event that caused the re-evaluation
    which flipped eligibility).
  - Keep FKs valid: when referenced CI rows are deleted, mark affected windows stale and rebuild.
- Non-goals:
  - Recording *all* reasons a PR is ineligible, or explaining the full conjunction of conditions.
    Attribution is to the triggering event (the last missing piece / first broken condition), not
    a full eligibility audit trail.
  - Retaining attribution history across rebuilds. Each rebuild produces a fresh, internally
    consistent set of windows. Attribution from prior computations is not preserved.
  - Denormalizing CI context names onto `QueueWindow` for deletion resilience. Since windows are
    rebuilt after CI deletion, denormalization would describe a window in the *new* computation
    where the triggering event may no longer exist or the window may not exist at all.

## Proposed Design

### Event type discriminator

A new `TextChoices` enum `QueueWindowEventType` with values:

| Value | Triggering FK | Notes |
|---|---|---|
| `REQUIRED_LABEL_ADDED` | `timeline_event` | A required label was added |
| `REQUIRED_LABEL_REMOVED` | `timeline_event` | A required label was removed |
| `FORBIDDEN_LABEL_ADDED` | `timeline_event` | A forbidden label was added |
| `FORBIDDEN_LABEL_REMOVED` | `timeline_event` | A forbidden label was removed |
| `CI_PASSED` | `check_run` or `status_context` | CI state flipped to eligible |
| `CI_FAILED` | `check_run` or `status_context` | CI state flipped to ineligible |
| `PR_OPENED` | `timeline_event` | PR reopened (or opened fresh) |
| `DRAFT_CONVERTED` | `timeline_event` | PR converted from draft to ready |
| `CONVERTED_TO_DRAFT` | `timeline_event` | PR converted to draft |
| `PR_CLOSED` | `timeline_event` | PR closed or merged |
| `HEAD_PUSHED` | `timeline_event` | Force-push; resets CI state for new SHA |
| `INITIAL_STATE` | *(all null)* | PR was already eligible at t0 / ruleset `effective_from` |
| `RULESET_EFFECTIVE` | *(all null)* | Ruleset `effective_from` boundary reached |
| `UNKNOWN` | *(all null)* | Fallback; should not appear in practice |

Invariant: at most one of the three FK columns is non-null for a given open/close event, and
which one is set is determined by `event_type`.

### New fields on `QueueWindow`

```python
opened_by_event_type     = CharField(max_length=32, null=True, choices=QueueWindowEventType.choices)
opened_by_timeline_event = ForeignKey("syncer.PrTimelineEvent",    null=True, on_delete=SET_NULL, related_name="+")
opened_by_check_run      = ForeignKey("syncer.CommitCheckRun",      null=True, on_delete=SET_NULL, related_name="+")
opened_by_status_context = ForeignKey("syncer.CommitStatusContext", null=True, on_delete=SET_NULL, related_name="+")
opened_at_head_sha       = CharField(max_length=40, null=True)

closed_by_event_type     = CharField(max_length=32, null=True, choices=QueueWindowEventType.choices)
closed_by_timeline_event = ForeignKey("syncer.PrTimelineEvent",    null=True, on_delete=SET_NULL, related_name="+")
closed_by_check_run      = ForeignKey("syncer.CommitCheckRun",      null=True, on_delete=SET_NULL, related_name="+")
closed_by_status_context = ForeignKey("syncer.CommitStatusContext", null=True, on_delete=SET_NULL, related_name="+")
closed_at_head_sha       = CharField(max_length=40, null=True)
```

`closed_by_*` and `closed_at_head_sha` are null for windows that are still open (`to_ts = None`).

All FK columns use `on_delete=SET_NULL`. If a referenced row is deleted out-of-band, the FK
silently becomes null. The rebuild-on-deletion mechanism below (see Invariants) ensures that
this silent nullification triggers a rebuild, which either removes the window or re-attributes
it to the surviving CI row — so the null state is transient in practice.

### Logic changes in `_queue_windows_with_rules`

At each boundary point the function already knows which event caused the re-evaluation. The
change is to thread that context through so that when `on_queue` flips:
- the opening event is captured (event type + FK) on the new window
- the closing event is captured (event type + FK) on the window that just closed

The computed window objects (currently plain dataclasses or dicts) gain attribution fields
(event type, FK, and head SHA for each of open and close) which are populated during replay
and then persisted by `rebuild_queue_windows_for_ruleset`. The current head SHA is already
tracked across revision boundaries in the replay, so `opened_at_head_sha` /
`closed_at_head_sha` require no additional lookups.

### Interaction with the CI expire task

When `expire_stale_ci_for_repo_task` (passes 3/4) deletes superseded `CommitCheckRun` or
`CommitStatusContext` rows, it must:
1. Identify PRs whose windows reference any of the to-be-deleted row IDs via
   `opened_by_check_run_id IN (...)` or `closed_by_check_run_id IN (...)` (and analogously for
   status contexts) *before* the deletion.
2. Both mark those PRs' build state as stale *and* directly enqueue `rebuild_queue_windows_for_pr`
   for them. Doing both provides defence in depth: the stale marker ensures the periodic sweep
   catches any PR that slips through if the enqueued task fails, while the direct enqueue
   triggers a prompt rebuild rather than waiting for the next sweep interval.

After rebuild the windows will reflect only the surviving (latest) CI row for each
`(head_sha, name)`. The effect is that transient failures that were rerun to success disappear
from window history — which is the correct behavior given the expire task's intentional lossiness.

Passes 1/2 (stale pending rows) apply a similar obligation: any window whose FK points to a
pending-phantom row being deleted must be marked stale and enqueued for rebuild.

## Subtleties / Invariants

**I1 — Windows are a pure function of current DB inputs.**
After any mutation to the inputs (timeline events, CI rows, rulesets), the stored windows must
exactly match what `_queue_windows_with_rules` would produce if run now. This is the existing
design intent. This plan extends it to cover CI row deletions.

**I2 — Rebuild-on-deletion must be triggered explicitly.**
The existing stale-detection covers `gh_updated_at` bumps, ruleset changes, and revision
version changes. It does NOT fire when the expire task deletes a CI row. The expire task must
both mark affected PRs stale and directly enqueue them for rebuild before deleting rows.
Doing both provides defence in depth: the stale marker ensures the periodic sweep catches any
PR whose direct rebuild task fails; the direct enqueue avoids waiting for the next sweep cycle.
Without either mechanism, I1 is violated silently with no recovery path.

**I3 — Attribution FKs describe the computation that produced the window, not an absolute
historical record.**
After a rebuild, a window that previously closed because of check run A (now deleted) either
no longer exists or is re-attributed to the surviving check run. The FK on the new window is
always valid relative to the current DB state.

**I4 — The expire task is intentionally lossy; windows should reflect that.**
Transient failures that were rerun to success, and then had their failure row deleted, will
disappear from window history after the next rebuild. This is correct and expected: the queue
window history reflects the *retained* CI data, not the unretained data.

**I5 — TimelineEvent FKs are stable; CommitCheckRun FKs are not.**
`TimelineEvent` rows are not subject to deletion by any existing task. `opened_by_timeline_event`
/ `closed_by_timeline_event` FKs are therefore safe and will not be nullified. Only the CI FKs
require the rebuild-on-deletion guarantee.

**I6 — Synthetic boundaries always have null FKs; `*_at_head_sha` is still populated.**
Windows that open due to `INITIAL_STATE` or `RULESET_EFFECTIVE` have no associated DB event row,
so all three FK columns are null. The `event_type` discriminator distinguishes "null because
synthetic" from "null because the row was deleted" — do not conflate these in offline analysis.
`opened_at_head_sha` is still populated for synthetic opens, providing the only handle on which
revision was current at that boundary.

**I7 — Attribution is to the triggering event, not a complete eligibility explanation.**
Eligibility is a conjunction (open, not-draft, required labels, forbidden labels, CI). Attribution
records the single event whose processing caused `on_queue` to flip — the "last missing piece"
when opening, the "first broken condition" when closing. It does not record all conditions that
happened to be satisfied or unsatisfied at the boundary.

## Implementation Plan (Chunks)

1. **W1 — Model + migration**
   - Add `QueueWindowEventType` enum to `qb_site/analyzer/models/`.
   - Add eight new nullable fields to `QueueWindow` as described above.
   - Generate and commit migration.
   - Update `038-commit-ci-row-expiry-and-superseded-cleanup.md` to note that FKs now exist.

2. **W2 — Logic changes in `_queue_windows_with_rules`**
   - Thread triggering-event context through the boundary-event replay loop.
   - Populate attribution fields on computed window objects when `on_queue` flips.
   - No DB writes in this chunk; purely the computation layer.

3. **W3 — Persist attribution in `rebuild_queue_windows_for_ruleset`**
   - Pass attribution fields through to the upsert path.
   - Confirm existing windows without attribution (pre-migration) are overwritten correctly on
     next rebuild.

4. **W4 — Rebuild-on-deletion in expire task**
   - In `expire_stale_ci_for_repo_task`, before each CI deletion pass, query `QueueWindow` for
     rows referencing the to-be-deleted IDs.
   - For affected PRs: mark build state stale AND enqueue `rebuild_queue_windows_for_pr` directly.
   - Cover all four passes.

5. **W5 — Tests**
   - Unit tests for `_queue_windows_with_rules` attribution:
     - window opens/closes due to required label added/removed → `REQUIRED_LABEL_ADDED` /
       `REQUIRED_LABEL_REMOVED`, `timeline_event` FK set
     - window opens/closes due to forbidden label added/removed → `FORBIDDEN_LABEL_ADDED` /
       `FORBIDDEN_LABEL_REMOVED`, `timeline_event` FK set
     - window opens/closes due to CI check run → `CI_PASSED` / `CI_FAILED`, `check_run` FK set
     - window opens/closes due to CI status context → `CI_PASSED` / `CI_FAILED`,
       `status_context` FK set
     - synthetic open (INITIAL_STATE, RULESET_EFFECTIVE) → all FKs null, correct `event_type`
     - `opened_at_head_sha` / `closed_at_head_sha` match the active revision at transition time
     - open window → `closed_by_*` and `closed_at_head_sha` all null
   - Integration test for expire task rebuild-on-deletion:
     - build windows with a CI-attributed open/close
     - run expire task that deletes the referenced check run
     - assert affected PRs have build state marked stale AND are enqueued for rebuild
     - assert rebuilt windows are consistent with surviving CI rows (I1)

## Operational Notes

### Deploying to an existing install

- The migration adds ten nullable columns to `prqwindow` (eight FKs + two SHA fields). It is a
  pure `ALTER TABLE ... ADD COLUMN` with no backfill and no locking beyond the column additions;
  safe to run against a live database.
- After migration, all existing `PRQueueWindow` rows will have all attribution fields null. This
  is the correct initial state — null means "not yet attributed" until the next rebuild.
- Once the migration is deployed, the normal `analyzer.rebuild_queue_windows_sweep` will
  progressively populate attribution as it processes each PR. No explicit backfill command is
  needed; the sweep already rebuilds all windows that have a stale build state or that have
  never been built.
- If faster full attribution is needed (e.g. to unblock analysis), trigger a full sweep with a
  broader staleness cutoff, or use the `backfill_queue_window_build_states` management command
  to mark all build states stale and let the sweep pick them up.

### Pitfalls when regenerating queue windows

- **Null vs. synthetic after migration**: Until a window is rebuilt post-migration, null
  `opened_by_event_type` means "pre-attribution", not `INITIAL_STATE`. After rebuild,
  `INITIAL_STATE` windows will have that value set explicitly. Offline analysis should treat
  `opened_by_event_type IS NULL` as "not yet rebuilt" rather than as a specific event type.
- **Attribution reflects retained data, not full history**: If the expire task has already run
  and deleted superseded CI rows, rebuilding windows will produce attribution consistent with
  the *surviving* data only. Transient failures that were deleted will not appear in window
  history. This is correct behavior (I4) but worth noting if historical window shapes change
  after a rebuild.
- **Large-repo rebuild cost**: A full rebuild of queue windows for a large repo (e.g. mathlib4)
  will process many PRs. The existing sweep task handles this incrementally and is the preferred
  path. Avoid triggering a bulk direct rebuild outside the sweep unless debugging a specific PR.
- **FK pointing to a CI row that has since been updated**: `CommitCheckRun` rows are mutable
  (upserted in place on the same `github_node_id`). An FK on a window correctly identifies
  *which check run* caused the transition, but the row's current `conclusion` field may differ
  from its value at `from_ts` if the check run was later updated. Use `from_ts` together with
  the check run's `gh_completed_at` to recover the historical state.

## Validation Plan
- Unit tests as above (W5).
- Manual spot-check: pick a PR with a known label-driven window and a CI-driven window; verify
  attribution fields match the expected events from raw timeline/CI tables.
- Run `bash scripts/repo_check_compose.sh` after each chunk.

## Progress Notes
- 2026-04-03: Initial living plan drafted. Captures model design, invariants, and interaction
  with CI expire task following design discussion. Updated to split label event types into four
  distinct values, add `opened_at_head_sha` / `closed_at_head_sha` fields, and clarify that the
  expire task must both mark PRs stale and directly enqueue rebuilds.

## Finalization Notes
- After implementation, collapse into a concise ADR capturing the final schema, the invariants
  (I1–I7), and the expire-task obligation. Remove chunk-by-chunk rollout detail.
- Update `038-commit-ci-row-expiry-and-superseded-cleanup.md` to reference this doc and note
  the FK dependency.

## References
- `docs/design-decisions/010-queue-windows-first.md`
- `docs/design-decisions/038-commit-ci-row-expiry-and-superseded-cleanup.md`
- `qb_site/analyzer/models/queue_window.py`
- `qb_site/analyzer/services/queue_windows.py` — `_queue_windows_with_rules`,
  `rebuild_queue_windows_for_ruleset`
- `qb_site/syncer/models/commit_check_run.py`
- `qb_site/syncer/models/commit_status_context.py`
- `qb_site/syncer/models/pr_timeline_event.py`
- `qb_site/syncer/tasks/sync_tasks.py` — `expire_stale_ci_for_repo_task`
