# Malformed PRRevision Windows: Clamp Head History to PR Creation

Status: accepted. Builder fix, backstop, self-healing cleanup, and test matrix
landed; deploy/rollout pending (see Operational Notes).

## Context

- `PRRevision` rows are the analyzer's per-PR head-SHA history — windows
  `[from_ts, to_ts)` during which `head_sha` was the PR's head — built by
  `rebuild_pr_revisions` (`analyzer/services/revisions.py`) from
  `HEAD_FORCE_PUSHED` timeline events and commit-scoped CI first-seen. They feed
  the queue-window builder's head-aware CI gating (`queue_windows._head_sha_at_time`).
  See decisions 012, 013, 047.
- Intended invariant: windows are contiguous and strictly increasing, every
  `to_ts > from_ts`, with exactly one open-ended window (`to_ts IS NULL`) whose
  head equals `pr.head_sha`.
- **Bug.** Both builders anchor the first window's `from_ts` at `pr.gh_created_at`.
  But `_collect_ci_first_seen` gathers commit-scoped CI repo-wide by head SHA with
  no lower bound, and a PR's head commits are routinely pushed to the branch — and
  CI-triggered — *before* the PR is opened. When a head's first-seen precedes
  `gh_created_at`, the first window becomes `[gh_created_at, <earlier first-seen>)`:
  a backward window `to_ts < from_ts` whose head is the PR's initial commit.
- **Confirmed on production** (`leanprover-community/mathlib4`, 2026-06-22,
  read-only query): **1983** malformed rows across 1983 PRs; every one has
  `from_ts == gh_created_at`, `to_ts < gh_created_at`, zero force-push events, and
  `archive_imported_at` null (live data). The `#17574`/`#17448` examples are
  ordinary PRs whose commits were committed/pushed seconds-to-weeks before the PR
  was opened (e.g. `#17574`'s head was committed 18 s before the PR opened).
- **Two impact classes:**
  - **Trailing 250** — the malformed row also holds `max(from_ts)` (because
    `gh_created_at` is the PR's latest signal, so `built_through_ts ==
    gh_created_at`). For `at >= gh_created_at`, `_head_sha_at_time` selects it,
    sees `at >= to_ts`, and returns `(None, True)` → CI gated as `missing` against
    a null head (`queue_windows.py:288-289`). It also escapes the append delete
    sweep (`append_from_ts = max(from_ts)` → empty derived tail → `noop`), so a
    plain `rebuild_revisions` never repaired it. 212 closed / 14 merged / 24 open.
  - **Mid-chain 1733** — the current-head window is dated after creation, so
    current-head resolution is unaffected; the backward window only distorts
    head-lookup in the brief interval around `gh_created_at` (historical).
- Queue *time* accounting is **not** corrupted: the queue-window builder clamps to
  `t0 = pr.gh_created_at` and ignores any revision boundary before it
  (`queue_windows.py:435,573-575`); the head at `t0` is the latest revision with
  `from_ts <= t0`.
- **Not out-of-order CI re-runs, and not archive-related.** Both earlier
  hypotheses were refuted by the data: out-of-order-CI reproductions self-heal via
  #177's prefix-mismatch→full fallback, and all 1983 rows are live (not
  archive-imported), so the `committedDate` synthesis in
  `archive_import._split_contexts` is not involved.

## Decision

Clamp head-SHA history to PR creation so windows never start before the PR exists.

- **`_build_ci_head_windows`:** collapse every head whose CI first-seen precedes
  `gh_created_at` into a single window that *starts at* `gh_created_at`, carrying
  the last such head (the head at creation time). Heads first seen at/after
  creation keep their own boundary.
- **`_build_force_push_head_windows`:** drop any segment ending at/before
  `gh_created_at`, and clamp the start of the segment covering creation to
  `gh_created_at` (the symmetric seg-0 inversion).
- **`_normalize_windows` backstop:** after `_ensure_current_head_window`, drop any
  window with `to_ts <= from_ts` or a non-increasing `from_ts`, then re-stitch
  contiguity. A no-op on correct builder output; guards against future regressions
  in either builder.
- **Self-healing cleanup:** `rebuild_pr_revisions` forces a full rebuild whenever a
  malformed row exists for the PR. Combined with the clamp, the rebuild replaces it
  with correct windows and converges (no malformed row → no-op thereafter). The
  existing `analyzer.rebuild_revisions_sweep` therefore repairs all 1983 rows on
  deploy with no separate command.

## Consequences

- The earliest window starts exactly at `gh_created_at`, and the `max(from_ts)`
  row is always the open-ended current-head window — so `_head_sha_at_time` and the
  `head_mismatch` guard agree, and the trailing-250 null-head misgate is fixed.
- The append→`noop` escape is closed for new builds; legacy malformed rows are
  healed by the self-healing full rebuild.
- Pre-creation/superseded branch commits are no longer recorded as `PRRevision`
  rows. They were never the open PR's head, and no consumer needs them (queue
  gating clamps to `t0`; the snapshot reads only the trailing head; CI-by-SHA
  backfill candidates already have CI).
- Clamp output `[gh_created_at, head, None)` matches the force-push seg-0 anchor,
  so CI-only→force-push transitions are clean appends (no forced full rebuild, no
  degenerate windows).
- Queue windows and total-time-on-queue are unchanged (already clamped to `t0`).

## Invariants

- Per PR: rows ordered by `from_ts` are contiguous (`to_ts[i] == from_ts[i+1]`),
  strictly increasing, each `to_ts > from_ts`, with exactly one `to_ts IS NULL`
  whose `head_sha == pr.head_sha` (when set).
- The earliest window's `from_ts == pr.gh_created_at`; the `max(from_ts)` row is
  the open-ended current-head window.

## Operational Notes

- Status: builder fix + backstop + self-healing cleanup + test matrix landed;
  deploy pending.
- Rollout: deploy; `analyzer.rebuild_revisions_sweep` full-rebuilds and heals all
  1983 rows; the queue-window sweep then picks up the `revision_version` bumps. No
  migration and no new management command are required.
- Verify after deploy: re-run the confirmation query (Appendix; expect
  `malformed rows: 0` and `open-ended == head_sha`) and spot-check the 24 open PRs
  for on/off-queue agreement between the snapshot candidate path and the
  queue-window path (the 047 consistency check).
- Tests: `analyzer/tests/test_pr_revisions_clamp.py` exercises both builders across
  all timing orders relative to `gh_created_at` (before / mixed / after / exact /
  duplicate, plus combinatorial sweeps), the `_normalize_windows` backstop,
  self-heal of the trailing and mid-chain shapes, and the CI→force-push transition.
  The existing `test_pr_revisions.py` (normal post-creation timings) is unaffected.

## Alternatives

- **Extend-back** (anchor window-0 at `min(gh_created_at, earliest first-seen)`):
  identical queue results and a smaller diff, but it keeps semantically-meaningless
  pre-creation windows, forces a full rebuild to tear them down on the first later
  force push, and produces a degenerate zero-width force-push seg-0 when
  `first_fp.occurred_at < gh_created_at`. Rejected in favor of clamp-to-creation's
  uniform, append-clean force-push behavior.
- **Restructure the append delete-sweep scoping** (stop keying on `max(from_ts)`):
  unnecessary once the clamp prevents the malformed row and self-healing repairs
  legacy rows.
- **Record pre-creation/intermediate heads, or add a commit/push-time column:**
  out of scope (see 047).

## References

- Decisions: 012, 013, 023/045 (CI gating / watermark), 047.
- Code: `analyzer/services/revisions.py`; `analyzer/services/queue_windows.py:232-289`.

## Appendix: read-only verification query

Run on a one-off dyno; read-only. Post-fix it should report `malformed rows: 0`.

```bash
heroku run --app queueboard-backend --no-tty -- \
  sh -c 'export PYTHONPATH=$PWD/qb_site:$PWD${PYTHONPATH:+:$PYTHONPATH}; \
         exec python qb_site/manage.py shell' \
  < query.py
```

```python
from django.db.models import F
from analyzer.models import PRRevision

mal = list(PRRevision.objects.filter(to_ts__isnull=False, to_ts__lt=F("from_ts")))
print("malformed rows (to_ts < from_ts):", len(mal))  # expect 0 post-fix
pr_ids = {r.pull_request_id for r in mal}
trailing = sum(
    1
    for r in mal
    if r.from_ts
    == PRRevision.objects.filter(pull_request_id=r.pull_request_id)
    .order_by("-from_ts", "-seq", "-id")
    .values_list("from_ts", flat=True)
    .first()
)
print(f"distinct PRs: {len(pr_ids)}  trailing (hold max-from_ts): {trailing}")
```
