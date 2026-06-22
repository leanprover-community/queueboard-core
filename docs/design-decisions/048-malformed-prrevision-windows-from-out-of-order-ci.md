# Malformed PRRevision Windows From Out-of-Order CI Timestamps

Status: living implementation plan (fix not yet implemented).

## Context

- `PRRevision` rows are the analyzer's per-PR head-SHA history, built by
  `rebuild_pr_revisions` (`analyzer/services/revisions.py`) from force-push
  timeline events and CI-bearing commits. See decisions 012 (head changes),
  013 (incremental build state), and 047 (current-head-without-CI).
- Each row is a window `[from_ts, to_ts)`. The intended invariant: windows are
  contiguous and non-overlapping, ordered by time, with exactly one open-ended
  window (`to_ts IS NULL`) whose head equals `pr.head_sha`.
- Consumers assume `from_ts` is monotonic with the head chain:
  - `queue_windows._head_sha_at_time(pr, at)` resolves the head at `at` as the
    window with the greatest `from_ts <= at`, returning `None` if that window's
    `to_ts <= at` (`queue_windows.py:243-249`).
  - The `head_mismatch` guard in `rebuild_pr_revisions` and our triage scripts
    treat "the trailing revision" as `order_by('-from_ts','-seq','-id').first()`
    (`revisions.py:457`).

## Evidence (verified 2026-06-22, during the #177 rollout)

- After deploying #177 and running `rebuild_revisions` across all states on
  `leanprover-community/mathlib4`, 250 PRs (212 closed / 14 merged / 24 open)
  still reported trailing-head ≠ `pr.head_sha` under a max-`from_ts` definition.
- Confirmation across all 250:
  - open-ended window (`to_ts IS NULL`) == `pr.head_sha`: **250/250**.
  - PRs carrying a malformed window (`to_ts < from_ts`): **250/250**, exactly
    one each.
- In every sampled PR the malformed row is `seq=0` (the PR's *initial* head); its
  `from_ts` equals the stored `built_through_ts` (the latest signal time), while
  its `to_ts` correctly chains to `seq=1`'s `from_ts`. Example
  (`mathlib4#17448`): `from=2024-10-05 15:19:40 head=a14fe3 to=2024-10-05
  00:58:34`, alongside the correct current window `from=15:17:54 head=39ce6a
  to=None` (== `pr.head_sha`).

## Impact

- **Not cosmetic.** For any `at >= built_through_ts`, `_head_sha_at_time` selects
  the malformed window (it holds the max `from_ts`), observes `at >= to_ts`, and
  returns `(None, True)`. `_ci_required_contexts_state` then returns `missing`
  (`queue_windows.py:288-289`) instead of evaluating the true current head.
  - **Open PRs (24):** "now" CI is evaluated against a null head. Under
    mathlib4's `no_required_failures` gating, `missing` ⇒ on-queue, so a PR whose
    real head carries a required **failure** can be wrongly kept on-queue. Where
    the real head is passing/pending, there is no practical difference.
  - **Closed/merged (226):** off-queue regardless; impact is skewed historical
    queue-time/attribution near `built_through_ts`.
- **Predates #177.** The #177 rebuild correctly set the open-ended head but did
  not remove the malformed row — it is reproduced/preserved on each rebuild (see
  Root cause), so a plain `rebuild_revisions` does not self-heal it.
- **Rollout correction:** on/off-queue is *not* guaranteed correct for the
  affected open PRs; the 24 should be verified.

## Root cause (partially pinned — reproduce before coding)

- A non-current head receives a `from_ts` later than its own `to_ts` — a
  negative-duration window — because window boundaries are derived from CI
  first-seen timestamps (`_collect_ci_first_seen`) that can arrive out of
  chronological order relative to the head's position in the chain (a re-run or
  late status for an early head lands at `built_through_ts`).
- The write path does not enforce `to_ts IS NULL OR to_ts > from_ts`, and the
  append/delete reconciliation (`revisions.py:505-564`) is scoped by
  `append_from_ts = max(from_ts)`. A malformed row that holds the max `from_ts`
  can therefore both violate the monotonic-window assumption and escape the
  delete sweep.
- The exact producing path is **not** yet reproduced from first principles: the
  three window builders (`_build_force_push_head_windows`,
  `_build_ci_head_windows`, `_ensure_current_head_window`) each independently
  forbid `to_ts < from_ts`, so the row most likely arises from the *interaction*
  of out-of-order CI timestamps with append-mode prefix preservation. Pin it with
  the repro step below before implementing.

## Goals / Non-Goals

- Goals:
  1. The builder never persists a malformed or overlapping window.
  2. Current-head resolution is robust to timestamp skew.
  3. Existing malformed rows are cleaned up.
  4. Affected open PRs are verified/repaired.
- Non-Goals:
  - Recording intermediate transient heads (still out of scope; see 047).
  - Adding a commit/push-time column (rejected in 047).

## Proposed design

1. **Builder invariant (core).** In `rebuild_pr_revisions`, after computing
   `expected` and before persisting:
   - validate that `from_ts` strictly increases, each `to_ts` (when set) equals
     the next window's `from_ts`, and every `to_ts > from_ts`;
   - drop or merge any window whose CI-derived `from_ts` would invert ordering (a
     head whose only signal is a late, out-of-order CI timestamp must not open a
     backwards window);
   - guarantee exactly one open-ended window carrying `pr.head_sha` (already done
     by `_ensure_current_head_window`).
2. **Full reconciliation.** Ensure the delete step removes every row not in the
   freshly computed `expected`, independent of `append_from_ts` — or derive
   `append_from_ts` from the open-ended/chain tail rather than `max(from_ts)` — so
   a malformed tail cannot survive an append rebuild.
3. **Robust current-head resolution (defense in depth).** Define "current head"
   as the open-ended window (`to_ts IS NULL`) in the `head_mismatch` guard
   (`revisions.py:457`) and document the monotonic-window invariant for
   `_head_sha_at_time`. With invariant #1 holding, max-`from_ts` and the
   open-ended window coincide.
4. **Cleanup pass.** A one-off normalize step (management command or
   self-healing on rebuild) that deletes malformed rows and rebuilds. Required
   because a plain `rebuild_revisions` reproduces the row today.

## Invariants

- For each PR with revisions, rows ordered by `from_ts` are contiguous
  (`to_ts[i] == from_ts[i+1]`), strictly increasing, each `to_ts > from_ts`,
  with exactly one `to_ts IS NULL` whose `head_sha == pr.head_sha` (when
  `pr.head_sha` is set).

## Implementation plan (chunks)

1. **Reproduce.** Dump timeline events + CI first-seen for ≥2 affected PRs
   (`mathlib4#17574` is the simplest 2-window case; `#17448` a 4-window one) and
   pin the exact producer; turn it into a regression fixture.
2. **Builder fix** (designs #1, #2) with unit tests asserting the invariant on
   the repro fixtures and on synthetic out-of-order-CI inputs.
3. **Robust current-head** (design #3) with tests.
4. **Cleanup tooling** (design #4): dry-run + apply.
5. **Rollout:** deploy, run cleanup across repos, re-run the confirm query
   (expect `malformed windows: 0`), and verify the 24 open PRs.

## Validation plan

- Unit: extend `analyzer/tests/test_pr_revisions.py` — late/out-of-order CI on an
  early head ⇒ no malformed window; `_head_sha_at_time(now)` returns the current
  head.
- Data: confirm query reports `malformed windows: 0` and `open-ended == head_sha`
  for all flagged PRs.
- Spot-check the 24 open PRs for on/off-queue agreement between the snapshot
  candidate path and the queue-window path (the consistency check from 047).

## Progress notes

- 2026-06-22: Discovered during the #177 rollout. Verified evidence and impact
  above; agreed fix direction (invariant + reconciliation + cleanup). Producer
  path not yet reproduced. Temporary triage scripts
  (`rebuild_affected_revisions.py`, `diagnose_stale_heads.py`,
  `confirm_current_head.py`) were used to gather this evidence and then removed.

## References

- Decisions: 012, 013, 023/045 (CI gating / watermark), 047.
- Code: `analyzer/services/revisions.py`; `analyzer/services/queue_windows.py:232-289`.

## Appendix: read-only diagnostic queries

These are the queries used to gather the evidence above and to verify the fix.
Both are read-only. Run on a one-off dyno via:

```bash
heroku run --app queueboard-backend --no-tty -- \
  sh -c 'export PYTHONPATH=$PWD/qb_site:$PWD${PYTHONPATH:+:$PYTHONPATH}; \
         exec python qb_site/manage.py shell' \
  < query.py
```

### A. Confirm / measure (proves the bug; verifies the fix)

The single clearest invariant check is the count of malformed rows across the
repo — it should be **0** after the fix:

```python
from django.db.models import F, OuterRef, Subquery
from analyzer.models import PRRevision
from syncer.models import PullRequest

# Primary invariant: no window may have to_ts < from_ts.
print("malformed rows (to_ts < from_ts):",
      PRRevision.objects.filter(to_ts__isnull=False, to_ts__lt=F("from_ts")).count())

# Classify the PRs flagged by the naive max-from_ts "trailing" definition
# (this is what surfaced the 250). Pre-fix expectation: flagged > 0, all with a
# correct open-ended head and exactly one malformed window. Post-fix: flagged == 0.
tail_head = (
    PRRevision.objects.filter(pull_request=OuterRef("pk"))
    .order_by("-from_ts", "-seq", "-id").values("head_sha")[:1]
)
flagged = (
    PullRequest.objects.filter(timeline_backfill_done=True)
    .exclude(head_sha__isnull=True).exclude(head_sha="")
    .annotate(tail_head=Subquery(tail_head))
    .exclude(tail_head__isnull=True).exclude(tail_head=F("head_sha"))
    .only("id", "number", "head_sha")
)
prs = list(flagged)
open_ok = open_bad = no_open = multi_open = with_malformed = 0
for pr in prs:
    head = (pr.head_sha or "").strip()
    open_rows = list(
        PRRevision.objects.filter(pull_request=pr, to_ts__isnull=True).values_list("head_sha", flat=True)
    )
    if not open_rows: no_open += 1
    elif len(open_rows) > 1: multi_open += 1
    elif (open_rows[0] or "") == head: open_ok += 1
    else: open_bad += 1
    if PRRevision.objects.filter(pull_request=pr, to_ts__isnull=False, to_ts__lt=F("from_ts")).exists():
        with_malformed += 1
print(f"flagged={len(prs)} open_ended==head_sha={open_ok} BAD={open_bad} "
      f"no_open={no_open} multi_open={multi_open} with_malformed={with_malformed}")
```

### B. Reproduce / pin the producer (chunk 1)

Dumps the exact inputs the builder sees for one PR, so the out-of-order CI
timestamp that creates the backwards window can be located. Note that the
malformed row's `from_ts` is expected to equal `built_through_ts`, which equals
`max(CI first-seen)`.

```python
from analyzer.services.revisions import _collect_ci_first_seen
from analyzer.models import PRRevision, PRRevisionBuildState
from syncer.models import PullRequest, PRTimelineEvent, PRTimelineEventType

REPO, NUM = "leanprover-community/mathlib4", 17574  # simplest 2-window case; also 17448
owner, name = REPO.split("/")
pr = PullRequest.objects.get(repository__owner=owner, repository__name=name, number=NUM)
st = PRRevisionBuildState.objects.filter(pull_request=pr).first()
print(f"head_sha={pr.head_sha} state={pr.state}")
print(f"gh_created_at={pr.gh_created_at} gh_updated_at={pr.gh_updated_at}")
if st:
    print(f"built_through_ts={st.built_through_ts} revision_version={st.revision_version} "
          f"builder_version={st.builder_version}")

print("\nforce-push events (occurred_at, before -> after):")
for ev in PRTimelineEvent.objects.filter(
    pull_request=pr, type=PRTimelineEventType.HEAD_FORCE_PUSHED
).order_by("occurred_at", "id"):
    print(f"  {ev.occurred_at}  {ev.before_sha} -> {ev.after_sha}")

first_seen, ci_latest = _collect_ci_first_seen(pr)
print("\nCI first-seen per head (the builder's view, ascending):")
for sha, ts in sorted(first_seen.items(), key=lambda kv: kv[1]):
    print(f"  {ts}  {sha}")
print(f"ci_latest={ci_latest}")

print("\nPRRevision rows (chronological):")
for seq, from_ts, head_sha, to_ts in PRRevision.objects.filter(pull_request=pr).order_by(
    "from_ts", "seq", "id"
).values_list("seq", "from_ts", "head_sha", "to_ts"):
    bad = "  <-- MALFORMED (to_ts < from_ts)" if (to_ts is not None and to_ts < from_ts) else ""
    print(f"  seq={seq} from={from_ts} head={head_sha} to={to_ts}{bad}")
```
