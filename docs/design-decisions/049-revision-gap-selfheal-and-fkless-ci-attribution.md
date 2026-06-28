# Revision-Gap Self-Heal and FK-less CI Attribution

Status: accepted. Builder self-heal extension + staleness-check fix landed with
regression tests.

## Context

The analyzer's `AnalyzerConvergenceSnapshot.windows_stale` metric sat at a small,
constant non-zero value for days on `mathlib4` (3 `(PR, ruleset)` pairs). The
`analyzer.rebuild_queue_windows_sweep` task reported the same PRs as
`prs_stale_ruleset` every run while rebuilding **zero** of them
(`windows_rebuilt: 0`, `prs_rebuilt_stale_ruleset: []`) — a permanent stale loop.

Investigation (PRs 38269, 21594, 20584) found two stacked defects:

1. **Non-contiguous `PRRevision` windows (root cause).** Each affected PR had a
   hole between two adjacent revision windows — one revision's `to_ts` strictly
   before the next revision's `from_ts` (and, on 21594, out-of-order/overlapping
   rows). All CI rows for every head were present in the DB; nothing was missing
   from GitHub. The gaps were legacy data produced by a pre-048 builder that
   dropped a head.

   Design 048 established the contiguity invariant (`to_ts[i] == from_ts[i+1]`)
   and a self-healing full rebuild, but the self-heal only triggered on a single
   **malformed** window (`to_ts < from_ts`). A *gap or overlap* where each window
   is individually valid was never detected, so — combined with the rebuild
   `noop` short-circuit for closed/inactive PRs whose trailing head already
   matches `pr.head_sha` — these rows re-noop forever and never heal. The builder
   version (`PR_REVISION_BUILDER_VERSION`) was not bumped when 048 landed, so that
   path did not force a rebuild either.

2. **FK-less CI attribution treated as a defect (the loop).** Inside a revision
   gap the queue-window builder cannot resolve a head SHA, so it computes
   `ci_state="missing"`. That flips queue eligibility (under
   `all_required_success`, missing ⇒ ineligible ⇒ the window closes as
   `CI_FAILED`; under `no_required_failures`, missing ⇒ eligible ⇒ the window
   opens as `CI_PASSED`). Because there is no CI row for the nonexistent head, the
   flip is attributed to CI with both CI FKs null
   (`_determine_ci_boundary_attribution` step 5).

   The sweep's staleness predicate (`_is_ruleset_stale_for_pr`) and the
   convergence collector both treated "CI `event_type` with both CI FKs null" as
   an inconsistency requiring backfill. But the builder *legitimately* produces
   that shape, and a rebuild reproduces it identically (`created=updated=deleted=0`),
   so the window was flagged stale, "rebuilt" to the same bytes, and re-flagged on
   the next sweep — forever.

A FK-less CI attribution is also produced legitimately outside revision gaps:
when CI rows for an old head are deleted by `syncer.expire_stale_ci_for_repo`
(doc 038), a rebuild can no longer tie the flip to a surviving row.

## Decision

1. **Extend the design-048 self-heal to detect non-contiguity, not just malformed
   windows.** `_revisions_need_recontiguation(pr)` returns True when persisted
   rows (ordered by `from_ts`) violate the invariant: any malformed window
   (`to_ts <= from_ts`), any gap/overlap (`to_ts[i] != from_ts[i+1]`), or a
   non-final window with a null `to_ts`. `rebuild_pr_revisions` forces a full
   rebuild when it returns True (before the `noop` short-circuit), so
   `_normalize_windows` re-stitches contiguity. It is a no-op on
   `_normalize_windows` output and therefore converges.

2. **Stop treating FK-less CI attribution as stale.** Remove the
   "CI `event_type` + both CI FKs null" clauses from the sweep prefilter, the
   sweep's exact per-PR check, and the convergence `windows_stale` collector. A CI
   `event_type` with null FKs is an accepted terminal state. Pre-migration windows
   with `opened_by_event_type IS NULL` are still flagged (a rebuild repairs them).

## Consequences

- Gappy/overlapping legacy revision rows now heal on the next rebuild that visits
  the PR, and the queue-window boundary at the former gap is re-derived against a
  real head with real CI (typically a `HEAD_PUSHED` or FK-bearing CI boundary).
- `windows_stale` no longer loops on legitimately FK-less CI windows. The genuine
  "expired CI FK" case is already covered by `expire_stale_ci_for_repo` nulling
  `windows_built_at` (doc 045 watermark path); a window-row-level signal is not
  needed and was the source of the loop.
- Detection cost: `_revisions_need_recontiguation` loads a PR's revision rows
  (ordered `from_ts`, `to_ts` only) once per non-noop rebuild decision, replacing a
  single `EXISTS` query. Revision counts per PR are small; acceptable.

## Operational Notes

- One-off recovery for the three known PRs was done by forcing a full revision
  rebuild (set `PRRevisionBuildState.dirty_from_ts`), then
  `rebuild_queue_windows_for_pr` + `record_queue_window_build_states`. No GitHub
  fetch was needed — the CI data was already present.
- The self-heal only repairs PRs that `rebuild_revisions_sweep` actually visits.
  Long-closed PRs that the sweep no longer scans may need a one-off backfill pass
  over PRs with gaps if full historical cleanup is desired.

## Alternatives

- *Introduce a distinct FK-less CI event type* (e.g. `CI_INVALIDATED`) and exclude
  it from the staleness check while keeping `CI_PASSED`/`CI_FAILED ⇒ FK` as a
  defect signal. More faithful data, but more invasive (new enum value, a data
  relabel) and unnecessary once the revision-gap root cause is fixed.
- *Bump `PR_REVISION_BUILDER_VERSION`* to force every PR through the normalizer
  once. Broader one-time churn than the targeted contiguity self-heal; rejected in
  favor of healing only the PRs that actually violate the invariant.
