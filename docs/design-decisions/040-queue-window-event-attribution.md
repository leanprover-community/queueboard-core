# Queue Window Event Attribution

## Context
- `PRQueueWindow` records the interval `[from_ts, to_ts)` during which a PR was on the queue
  under a given ruleset, but carries no information about *why* each transition happened.
- For offline analysis we want to know what caused each window to open and close — was it a
  label change, a CI result, a PR state change, etc.?
- Queue windows are already computed by replaying boundary events chronologically in
  `_queue_windows_with_rules` (`qb_site/analyzer/services/queue_windows.py`). At each
  boundary point the code already knows which event caused the re-evaluation; this decision
  captures that context in the stored rows.
- Boundary sources:
  - `PRTimelineEvent` (label add/remove, draft toggle, open/close, force-push)
  - `CommitCheckRun` / `CommitStatusContext` (CI completion/update)
  - Synthetic boundaries (PR creation, ruleset `effective_from`, revision changes)
- The expire task (`syncer.expire_stale_ci_for_repo`) deletes superseded CI rows
  (see `038-commit-ci-row-expiry-and-superseded-cleanup.md`). Doc 038 originally noted
  "there are no FKs pointing into `CommitCheckRun` or `CommitStatusContext` from other models"
  — this decision changes that.

## Decision
Add ten nullable fields to `PRQueueWindow` recording which event caused each window to open
and close. Use FK references to the triggering DB row (when one exists) plus a discriminator
`event_type` field. When referenced CI rows are deleted by the expire task, mark affected
windows stale and enqueue their PRs for rebuild before deletion.

Attribution is to the *triggering event* — the single boundary event whose processing caused
`on_queue` to flip — not a full eligibility explanation.

## Architecture

### Event Type Discriminator

A new `QueueWindowEventType` `TextChoices` enum:

| Value | FK column populated | Notes |
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
| `INITIAL_STATE` | *(all null)* | PR was already eligible at window start |
| `RULESET_EFFECTIVE` | *(all null)* | Ruleset `effective_from` boundary reached |
| `UNKNOWN` | *(all null)* | Defensive fallback; should not appear in practice |

At most one of the three FK columns is non-null for any open/close event; which one is set
is determined by `event_type`.

### New Fields on `PRQueueWindow`

```python
opened_by_event_type     = CharField(max_length=32, null=True, choices=QueueWindowEventType.choices)
opened_by_timeline_event = ForeignKey("syncer.PRTimelineEvent",   null=True, on_delete=SET_NULL, related_name="+")
opened_by_check_run      = ForeignKey("syncer.CommitCheckRun",     null=True, on_delete=SET_NULL, related_name="+")
opened_by_status_context = ForeignKey("syncer.CommitStatusContext",null=True, on_delete=SET_NULL, related_name="+")
opened_at_head_sha       = CharField(max_length=40, null=True)

closed_by_event_type     = CharField(max_length=32, null=True, choices=QueueWindowEventType.choices)
closed_by_timeline_event = ForeignKey("syncer.PRTimelineEvent",   null=True, on_delete=SET_NULL, related_name="+")
closed_by_check_run      = ForeignKey("syncer.CommitCheckRun",     null=True, on_delete=SET_NULL, related_name="+")
closed_by_status_context = ForeignKey("syncer.CommitStatusContext",null=True, on_delete=SET_NULL, related_name="+")
closed_at_head_sha       = CharField(max_length=40, null=True)
```

`closed_by_*` and `closed_at_head_sha` are null for windows that are still open (`to_ts = None`).

All FK columns use `on_delete=SET_NULL`. If a referenced row is deleted out-of-band the FK
silently becomes null; the rebuild-on-deletion mechanism (see below) ensures this state is
transient in practice.

Migration `0025_prqueuewindow_closed_at_head_sha_and_more.py` — pure `ADD COLUMN`, safe on
a live database with no backfill required.

### Attribution Computation (`_queue_windows_with_rules`)

The `_queue_windows_with_rules` function returns `List[AttributedQueueWindow]` (a new
dataclass wrapping `from_ts`, `to_ts`, `opened_by: WindowAttribution`, and
`closed_by: Optional[WindowAttribution]`). `WindowAttribution` holds `event_type`, at most
one FK object, and `head_sha`.

**Label-only path** — each boundary event is a `PRTimelineEvent`; `_timeline_event_type`
maps it to the correct `QueueWindowEventType` using the ruleset's required/forbidden label
sets. On each flip, the triggering event is captured directly.

**CI path** — `_determine_ci_boundary_attribution` resolves the triggering event by
priority:
1. CI eligibility changed **and** a CI event was present at this boundary → `CI_PASSED` /
   `CI_FAILED` with FK to the triggering check run or status context.
2. A timeline event was present at this boundary → corresponding timeline type.
3. A revision change occurred (HEAD_PUSHED) with no other event.
4. CI changed without an identifiable event → `UNKNOWN` (defensive; should not fire on
   clean data).

The current head SHA is already tracked across revision boundaries in the replay, so
`opened_at_head_sha` / `closed_at_head_sha` require no additional lookups.

`queue_windows_for_pr` strips attribution to preserve the `List[Tuple]` return type for
all existing callers outside the rebuild path.

### Persistence (`rebuild_queue_windows_for_ruleset`)

`_attribution_kwargs(attr, prefix)` unpacks a `WindowAttribution` into model field kwargs,
comparing FK objects by `_id` to avoid unnecessary DB fetches. Attribution fields are
always overwritten on both the create and update paths; they reflect the current computation
and are never carried forward from a previous rebuild.

### Rebuild-on-Deletion (`expire_stale_ci_for_repo_task`)

`_invalidate_queue_windows_for_ci_rows(check_run_ids, status_context_ids)` is called before
each of the four deletion passes with the IDs about to be deleted. It:
1. Queries `PRQueueWindow` for rows referencing any of the given IDs across all four CI FK
   columns (`opened_by_check_run_id`, `closed_by_check_run_id`, etc.).
2. Nulls `windows_built_at` on `PRQueueWindowBuildState` for the affected `(PR, ruleset)` pairs.
3. Enqueues `process_pr_task.delay(pr_id)` for each affected PR.

Both the stale mark and the direct enqueue are done (defence in depth): the stale marker
ensures the periodic sweep catches any PR whose direct rebuild task fails; the direct enqueue
avoids waiting for the next sweep cycle.

### Sweep Staleness Extension (`rebuild_queue_windows_sweep`)

Two new conditions are added to the `attribution_backfill` `Exists` subquery that feeds the
`needs_rebuild` prefilter:

- `opened_by_event_type IS NULL` — detects pre-migration windows (null means "not yet
  attributed", not a specific event type).
- `opened_by_event_type IN (CI_PASSED, CI_FAILED) AND opened_by_check_run IS NULL AND
  opened_by_status_context IS NULL` (and the same for `closed_by_*`) — detects the
  post-expire-task partial-failure state where the FK was SET_NULL but the event_type
  was not cleared.

The subquery uses the same `Exists` pattern as the existing `rollup_backfill` condition and
operates on indexed FK columns; it does not add a costly scan. `collect_convergence.py`
includes the same conditions in its `rollup_stale_pairs` query and must be kept in sync with
the sweep.

`has_attribution_backfill: bool` is threaded into `_is_ruleset_stale_for_pr`, which returns
`True` immediately when set. The outer prefilter is conservative (SQL `Exists` across all
rulesets); the inner check is per-ruleset exact.

## State Machine

### States

Queue membership under a given ruleset is a boolean `on_queue`, determined by the conjunction:

```
E = is_open ∧ ¬is_draft ∧ required_labels_satisfied ∧ ¬forbidden_labels_present ∧ ci_ok
```

where `ci_ok = True` when `require_ci_success = False`. The replay has two states:

- **OFF_QUEUE** (`E = False`)
- **ON_QUEUE** (`E = True`)

### Event Taxonomy

Events are classified by which conjunct they can affect:

| Category | Events | Conjunct affected |
|---|---|---|
| **S** — open/draft state | `REOPENED`, `CLOSED`, synthetic `closed_ts` | `is_open` |
| **S** — open/draft state | `READY_FOR_REVIEW`, `CONVERT_TO_DRAFT` | `is_draft` |
| **L_req** — required label | `LABELED[required]`, `UNLABELED[required]` | `required_labels_satisfied` |
| **L_fbd** — forbidden label | `LABELED[forbidden]`, `UNLABELED[forbidden]` | `¬forbidden_labels_present` |
| **CI_E** — CI event | `CommitCheckRun`, `CommitStatusContext` | `ci_ok` (directly) |
| **R** — revision | new `PRRevision` | `ci_ok` (indirectly, via `head_sha`) |
| **Synth** — synthetic | `t0` (PR creation), `effective_from` | initial evaluation point only |
| **I** — irrelevant | `LABELED/UNLABELED[other]`, non-required CI events | none |

Category I events can never change `E`. They may co-occur with meaningful events at the same
timestamp but must never influence attribution.

### Transitions and Attribution

**OFF_QUEUE → ON_QUEUE**

| Attribution | Triggering category | Notes |
|---|---|---|
| `INITIAL_STATE` | Synth (`t0` or `effective_from`) | `E` was already True at the initial evaluation point |
| `RULESET_EFFECTIVE` | Synth (`effective_from`) | `E` became True at the ruleset boundary (not yet emitted) |
| `PR_OPENED` | S (`REOPENED`) | `is_open` became True |
| `DRAFT_CONVERTED` | S (`READY_FOR_REVIEW`) | `is_draft` became False |
| `REQUIRED_LABEL_ADDED` | L_req (`LABELED[required]`) | missing required label supplied |
| `FORBIDDEN_LABEL_REMOVED` | L_fbd (`UNLABELED[forbidden]`) | blocking forbidden label removed |
| `CI_PASSED` | CI_E | `ci_ok` became True via a CI row |
| `HEAD_PUSHED` | R | new `head_sha`'s CI state is eligible (or CI unknown → later passes) |

**ON_QUEUE → OFF_QUEUE**

| Attribution | Triggering category | Notes |
|---|---|---|
| `PR_CLOSED` | S (`CLOSED` event or synthetic `closed_ts`) | `is_open` became False |
| `CONVERTED_TO_DRAFT` | S (`CONVERT_TO_DRAFT`) | `is_draft` became True |
| `REQUIRED_LABEL_REMOVED` | L_req (`UNLABELED[required]`) | required label removed |
| `FORBIDDEN_LABEL_ADDED` | L_fbd (`LABELED[forbidden]`) | forbidden label added |
| `CI_FAILED` | CI_E | `ci_ok` became False via a CI row |
| `HEAD_PUSHED` | R | new `head_sha`'s CI state is ineligible or missing |

### Priority Resolution for Co-occurring Events

When multiple event categories arrive at the same boundary timestamp `t`, priority determines
which is the triggering event (highest to lowest):

| Priority | Category | Rationale |
|---|---|---|
| 1 | **S** — state/draft change | Structurally decisive; a closed or drafted PR cannot be on queue regardless of other conditions |
| 2 | **CI_E** — CI event row present AND `ci_ok` changed | Direct CI gate flip, FK available |
| 3 | **L_req / L_fbd** — relevant label event | Direct eligibility-set change |
| 4 | **R** — revision change AND `ci_ok` changed | Indirect CI change via new `head_sha` |
| 5 | **CI_E** — `ci_ok` changed but no CI row identified | CI change without FK |
| 6 | **UNKNOWN** | Defensive fallback; should not appear on clean data |

Category I events are never attributed regardless of position in the event stream.

`closed_ts` (the synthetic `is_open = False` boundary from `pr.closed_at / pr.merged_at`) is
category S, priority 1, even when no `CLOSED` `PRTimelineEvent` row exists at that timestamp.

### Correct Priority Order in `_determine_ci_boundary_attribution`

Prerequisite: **Fix B1** must be applied to the boundary loop so that `last_tl_ev` always
holds the most significant event at the boundary (state/draft > relevant label > irrelevant).
With that guarantee, the function evaluates in this order:

1. **Synthetic close** — caller detected `t == closed_ts` → return `PR_CLOSED` directly,
   before calling this function.
2. **State/draft timeline event** — `last_tl_ev` is `CLOSED`, `REOPENED`,
   `READY_FOR_REVIEW`, or `CONVERT_TO_DRAFT` → return the corresponding type.
3. **CI changed AND CI row present** — `ci_ok` changed and `last_cr` or `last_sc` is set →
   return `CI_PASSED` / `CI_FAILED` with FK.
4. **Relevant label timeline event** — `last_tl_ev` is `LABELED` / `UNLABELED` for a
   required or forbidden label → return `REQUIRED_LABEL_*` / `FORBIDDEN_LABEL_*`.
5. **Revision changed** — `cur_rev is not pre_rev` → return `HEAD_PUSHED`.
6. **CI changed, no CI row** — `ci_ok` changed but no row available → return `CI_PASSED` /
   `CI_FAILED` without FK.
7. **UNKNOWN** — fallback.

Steps 2 and 4 together replace the current monolithic "timeline event present" check (Fix B2).
With Fix B1 in place, an irrelevant label can only reach step 4 when no significant event
exists, at which point the function correctly falls through to steps 5–7.

### GitHub event model vs. stored events

GitHub's timeline API distinguishes `MergedEvent` and `ClosedEvent`. When a PR is merged,
GitHub fires **both**; when it is simply closed without merging, only `ClosedEvent` is fired.
Our GraphQL queries request `ClosedEvent` but not `MergedEvent`. This is intentional:
`ClosedEvent.createdAt` equals `pr.mergedAt` for merged PRs, so we get the correct timestamp
for free and do not need a separate `MergedEvent` row.

For merged PRs, `pr.closed_at == pr.merged_at`. `closed_ts = pr.closed_at or pr.merged_at`
therefore always resolves to the merge timestamp regardless of which field is populated.

GitHub's `pr.closedAt` (stored as `PullRequest.closed_at` / `merged_at`) and
`ClosedEvent.createdAt` (stored as `PRTimelineEvent.occurred_at`) are generated at slightly
different instants for the same action. In practice `ClosedEvent.createdAt` is consistently
**1 second later** than `pr.closedAt`. The `ClosedEvent` is always present in the DB (for
fully-synced PRs); the timestamps simply do not match.

`pr.closed_at` / `pr.merged_at` on the `PullRequest` row are therefore the **reliable
source** for the close boundary; `ClosedEvent` is the **preferred but optional** source that
provides a storable FK.

**Interaction in the replay loop** (the 1-second gap matters):
- `t = closed_ts` (`pr.closed_at`): `state.is_open` is forced to False; the `CLOSED`
  timeline event is at `closed_ts + 1s`, so `_last_tl_ev = None`; `_determine_ci_boundary_attribution`
  falls to UNKNOWN. This is Bug A.
- `t = closed_ts + 1s` (the `CLOSED` event's own boundary): `state.is_open` is already
  False, `new_on = False`, `current_on = False` — no flip is detected and the event is
  silently dropped.

The `closed_ts` boundary is therefore never coincident with the `ClosedEvent` boundary.
`closed_ts` is always the operative boundary; the `CLOSED` timeline event always arrives one
second too late to provide attribution.

### Known UNKNOWN Bugs (pre-fix)

**Bug A — synthetic `closed_ts` without a `CLOSED` timeline event (~390 windows)**:
`closed_ts` is a category-S event (priority 1) but the function is called with `last_tl_ev =
None`, no CI change, and no revision change, so it falls to the UNKNOWN fallback. Fix: the
call site detects `t == closed_ts` and handles this directly before calling
`_determine_ci_boundary_attribution`, returning `PR_CLOSED`.

**Bug B — category-I label event shadowing the real cause (~3 800 windows)**:
Diagnosed from production data (300-window sample): 296 windows have a forbidden-label event
(the actual cause of the flip) AND an irrelevant label event at the same second; 4 windows
have an irrelevant label event co-occurring with a CI event. In both cases `_last_tl_ev = ev`
unconditionally overwrites on every iteration, so the irrelevant label (higher DB id, inserted
later in the same second) ends up as `_last_tl_ev` and `_timeline_event_type` returns
`UNKNOWN`.

Two coordinated fixes are required:

*Fix B1 — track the most significant event, not just the last.*
Change `_last_tl_ev = ev` in the boundary loop to prefer higher-significance events:
state/draft changes > relevant label events > irrelevant events. This ensures that when a
forbidden label and an irrelevant label land at the same second, `_last_tl_ev` holds the
forbidden label.

*Fix B2 — skip irrelevant events in `_determine_ci_boundary_attribution`.*
After Fix B1, `_last_tl_ev` will only be an irrelevant label when no significant timeline
event was present. Fix B2 handles the residual case (4 CI co-occurrence windows) and provides
defence in depth: even if an irrelevant label slips through, the function falls through to the
CI or revision check rather than returning `UNKNOWN`.

Fix B2 alone is insufficient for the 296 forbidden-label cases: if `_last_tl_ev` is the
irrelevant label and the forbidden-label event is discarded, the function can never produce the
correct attribution regardless of priority order.

Note also that the current priority 1 ("CI changed AND CI row present") fires before checking
for a state/draft timeline event. This means a simultaneous `CI_FAILED` and `CLOSED` event
would be attributed to `CI_FAILED` rather than `PR_CLOSED`. Under the corrected ordering,
state/draft changes take priority 1 even over CI rows with FKs.

## Consequences

- Every new or rebuilt `PRQueueWindow` carries attribution fields identifying the triggering
  event, suitable for offline analysis without rejoining raw timeline/CI tables.
- The `PRQueueWindow` table now holds nullable FKs into `CommitCheckRun` and
  `CommitStatusContext`. Bulk deletion of CI rows remains safe (FK uses `SET_NULL`), but the
  expire task now carries an explicit obligation to invalidate affected windows before deletion.
- After CI row deletion and rebuild, windows that depended on the deleted rows are recomputed
  from surviving data. They may change shape, merge with adjacent windows, or disappear
  entirely. They do **not** survive with `UNKNOWN` attribution — `UNKNOWN` is a defensive
  fallback for data inconsistencies in the current DB, not a consequence of deletion.
- Attribution backfill via the periodic sweep is automatic; no explicit backfill command is
  needed after deploying the migration.
- `CommitCheckRun` rows are mutable (upserted in place on the same `github_node_id`). An FK
  on a window correctly identifies *which check run* caused the transition, but the row's
  current `conclusion` may differ from its value at `from_ts` if the check run was updated
  later. Use `from_ts` together with the check run's `gh_completed_at` to recover the
  historical state.

## Subtleties / Invariants

**I1 — Windows are a pure function of current DB inputs.**
After any mutation to the inputs (timeline events, CI rows, rulesets), the stored windows
must exactly match what `_queue_windows_with_rules` would produce if run now. This decision
extends that invariant to cover CI row deletions.

**I2 — Rebuild-on-deletion must be triggered explicitly.**
The existing stale-detection covers `gh_updated_at` bumps, ruleset changes, and revision
version changes. It does NOT fire when the expire task deletes a CI row. The expire task must
both mark affected PRs stale and directly enqueue them for rebuild before deleting rows.
Without either mechanism, I1 is violated silently with no recovery path.

**I3 — Attribution FKs describe the computation that produced the window, not an absolute
historical record.**
After a rebuild, a window that previously closed because of check run A (now deleted) either
no longer exists or is re-attributed to the surviving check run. The FK on the new window is
always valid relative to the current DB state.

**I4 — The expire task is intentionally lossy; windows should reflect that.**
Transient failures that were rerun to success, and then had their failure row deleted, will
disappear from window history after the next rebuild. This is correct: the queue window
history reflects the *retained* CI data, not the unretained data.

**I5 — TimelineEvent FKs are stable; CI FKs are not.**
`PRTimelineEvent` rows are not subject to deletion by any existing task. `opened_by_timeline_event`
/ `closed_by_timeline_event` FKs will not be nullified. Only the CI FKs require the
rebuild-on-deletion guarantee.

**I6 — Synthetic boundaries have null FKs; `*_at_head_sha` is still populated.**
Windows that open due to `INITIAL_STATE` or `RULESET_EFFECTIVE` have no associated DB event
row, so all three FK columns are null. The `event_type` discriminator distinguishes
"null because synthetic" from "null because the row was deleted" or "null because
pre-migration". Offline analysis must not conflate these three cases.
`*_at_head_sha` is still populated for synthetic opens, providing a handle on which revision
was current at that boundary.

**I7 — Attribution is to the triggering event, not a complete eligibility explanation.**
Eligibility is a conjunction (open, not-draft, required labels, forbidden labels, CI).
Attribution records the single event whose processing caused `on_queue` to flip — the
"last missing piece" when opening, the "first broken condition" when closing. It does not
record all conditions that happened to be satisfied or unsatisfied at the boundary.

## Operational Notes

### Deploying to an existing install

The migration is pure `ADD COLUMN` with no backfill and no locking beyond the column
additions; safe to run against a live database. After migration all existing `PRQueueWindow`
rows have all attribution fields null. This means:

- `opened_by_event_type IS NULL` on a live row means "not yet rebuilt post-migration", not
  `INITIAL_STATE`. After rebuild, `INITIAL_STATE` windows have that value set explicitly.
  Offline analysis should treat `IS NULL` as "not yet attributed" rather than as a specific
  event type.
- The `attribution_backfill` sweep condition fires for all such rows, so the normal periodic
  sweep will progressively populate attribution without any manual intervention.
- If faster full attribution is needed, use `backfill_queue_window_build_states` to mark all
  build states stale; the sweep will then pick up all PRs on its next pass.

### Staleness coverage (updated table from doc 024)

| Staleness cause | Detection mechanism |
|---|---|
| New commit → `revision_version` bumped | `process_pr`; sweep revision lag |
| Label or state change → `gh_updated_at` bumped | `process_pr` unconditional rebuild; sweep `windows_built_at < gh_updated_at` |
| Ruleset definition changed | Sweep `windows_built_at < ruleset.updated_at` |
| New ruleset → missing build-state row | Sweep missing-state count |
| Existing windows missing rollup fields | Sweep `has_rollup_backfill` Exists subquery |
| Pre-migration windows (`opened_by_event_type IS NULL`) | Sweep `has_attribution_backfill` Exists subquery |
| CI FK nulled by expire task without rebuild | Sweep `has_attribution_backfill` Exists subquery |
| CI row deleted → rebuild triggered | `_invalidate_queue_windows_for_ci_rows` marks stale + direct enqueue |

### Attribution reflects surviving CI data after deletion

After the expire task deletes superseded CI rows, rebuilding windows produces attribution
consistent with the *surviving* data only. Historical window shapes may change if a deleted
failure row was previously the triggering event. This is correct behavior (I4) but relevant
for long-term offline analysis of window history.

## References
- `docs/design-decisions/010-queue-windows-first.md`
- `docs/design-decisions/024-per-ruleset-queue-window-build-state.md`
- `docs/design-decisions/038-commit-ci-row-expiry-and-superseded-cleanup.md`
- `qb_site/analyzer/models/queue_window.py` — `QueueWindowEventType`, `PRQueueWindow`
- `qb_site/analyzer/services/queue_windows.py` — `_queue_windows_with_rules`,
  `WindowAttribution`, `AttributedQueueWindow`, `_attribution_kwargs`,
  `rebuild_queue_windows_for_ruleset`
- `qb_site/analyzer/tasks/rebuild_queue_windows_sweep.py` — `attribution_backfill` subquery
- `qb_site/analyzer/tasks/collect_convergence.py`
- `qb_site/analyzer/migrations/0025_prqueuewindow_closed_at_head_sha_and_more.py`
- `qb_site/syncer/tasks/sync_tasks.py` — `_invalidate_queue_windows_for_ci_rows`,
  `expire_stale_ci_for_repo_task`
- `qb_site/analyzer/tests/services/test_queue_window_attribution.py`
- `qb_site/analyzer/tests/services/test_queue_window_attribution_persist.py`
- `qb_site/analyzer/tests/tasks/test_rebuild_queue_windows_sweep_attribution.py`
- `qb_site/syncer/tests/tasks/test_expire_stale_ci_attribution.py`
