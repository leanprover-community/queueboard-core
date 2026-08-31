# Reviewer Assignment Rate Limit (Rolling Weekly Intake Cap)

> Status: **Deployed to production, no limits set** (2026-08-28). Chunks 1–8 of the
> [Implementation Plan](#implementation-plan-chunks) have landed and shipped; every
> `max_new_assignments_per_week` is still `NULL`, so the push pipeline behaves exactly as it did
> before. That null default is the whole rollout mechanism — there is no feature flag (Open
> Question 5), so enabling is an edit to one field per reviewer and clearing it is the rollback.
> Still to do: a pilot cohort, and the engine simulation Open Question 3 asks for before any global
> default. The measurement probe was run against production before any code landed; see
> [Measured Baseline](#measured-baseline-2026-08-28), which confirms the premise and re-sized
> several claims. Origin: a Zulip thread (Christian Merten, with Yaël Dillies' earlier proposal and
> Bryan Gin-ge Chen) on making reviewer capacity limits actually bind.

## Context

- The only capacity limit on auto-assignment today is `core.ReviewerPreference.maximum_capacity`
  (`qb_site/core/models/reviewer_preference.py:39`, default 10) — a cap on **concurrently** assigned
  PRs. It is enforced as a single gate in the pure engine, `_reviewer_candidate_state`
  (`qb_site/analyzer/services/reviewer_assignment_engine.py:168-179`):

  ```python
  remaining = reviewer.maximum_capacity - current_weight
  if remaining > 0 and reviewer.auto_assign and not reviewer.temporary_break:
      available.append(reviewer.github_login)
  ```

- **The bound only bites if you let PRs pile up.** `maximum_capacity` limits *stock*, not *flow*.
  A reviewer who acts on newly-assigned PRs quickly frees the slot, and the next nightly run
  (`analyzer.refresh_reviewer_assignments` → `analyzer.propose_reviewer_assignments` /
  `analyzer.apply_reviewer_assignments`, ~00:30/00:45 UTC) refills them back up to
  `maximum_capacity`. The cap therefore only limits reviewers who *don't* act — the opposite of what
  a capacity limit should reward. This is Christian Merten's diagnosis, and it is correct — and now
  measured: three reviewers with `maximum_capacity=10` took **22, 23 and 30 new PRs in 30 days**
  ([Measured Baseline](#measured-baseline-2026-08-28)). The stock cap never bound their flow.

- The requested fix (Christian's proposal, anticipated as a deferred follow-up in
  `050-reviewer-assignment-acceptance-gate.md`: *"Throughput cap (reviewed X PRs in 30 days → pause
  auto-assign) — a separate capacity input"*) is to bound **flow**: limit how many *new* PRs a
  reviewer is assigned over a rolling period, independent of how fast they clear them.

- **A rolling-window rate is that limit, and its window length is the smoothing knob.** Christian's
  literal proposal is "N new PRs per rolling *month*". Bryan's objection — a reviewer could spend the
  whole month's budget in a week, then get nothing for three weeks — is really an objection to the
  *window being a month*. Shorten the window to a **week** and the same one-parameter mechanism
  becomes smooth by construction: a month's worth of intake cannot fit inside a 7-day window. So the
  limit is expressed as a single, human-legible rate:

  > **maximum new assignments per week** — a rolling 7-day cap on distinct newly-assigned PRs.

  This is *one* reviewer-facing number that is simultaneously the throughput limit and the smoother.
  It needs no separate "drip" or per-cycle knob (an earlier draft of this doc had a monthly cap plus a
  derived per-cycle drip; the weekly window replaces both — see [Alternatives](#alternatives-discarded)).

- **Catch-up is the pull side's job, not the push side's.** The one thing a short window gives up is
  *saving up* — a reviewer back from vacation cannot carry last week's unused budget into a bigger
  week. That is intentional: `053-on-demand-assignment-suggestions.md` (deployed 2026-08-27) already
  serves exactly that case. A reviewer with spare capacity *right now* runs `suggest-prs` /
  `/console/suggestions/` and claims work, and `053` **deliberately overrides every push throttle**
  (`maximum_capacity`, `auto_assign`, `away_until`). The whole system then reads cleanly:
  - **push side** = a steady, predictable, weekly-smoothed trickle;
  - **pull side (`053`)** = on-demand burst / catch-up.

  This rate limit is a fourth push throttle and must be overridden by `053` on the same footing (see
  [Interactions](#interactions-with-existing-pipelines)).

- **A per-reviewer assignment history already exists**, so no new log table is needed.
  `analyzer.ReviewerAssignmentApplication` (`046-apply-reviewer-assignments-in-django.md`) is an
  append-only, indefinitely-retained audit row per applied assignment
  (`qb_site/analyzer/models/reviewer_assignment_application.py`): `status='applied'`, `applied_at`,
  `run_date`, `repository`, `pr_number`, `reviewer_login`. It records every **system-mediated**
  assignment — the nightly auto direct-assign, confirm-mode accepts, and console pull-claims all go
  through the shared `assign_reviewer_and_record` (046) path. The one gap is a raw Zulip `assign`
  self-assign, which writes no application row (`053`'s known "audit asymmetry").

## Measured Baseline (2026-08-28)

`scripts/probe_054_rate_limit.sql`, run against production for `leanprover-community/mathlib4` (see
[Measuring first](#measuring-first-the-probe)). Everything here is measured. The design above was
written before any of it; where a number changed a claim, the claim was edited in place and the change
is listed under [What the measurement changed](#what-the-measurement-changed).

**The count source is healthy.** 859 rows / 839 `applied` over 2026-06-23 → 2026-08-28, covering 600
distinct PRs and 41 distinct reviewers at ~12.5 applied/day. Intake landed on **all 67 days** of that
span — the nightly apply has no gaps, so a rolling 7-day window is always fully populated. The other
20 rows are `skipped_recently_applied`; there are no failures, and **no `applied` row has a NULL
`applied_at`** (§1c), so the window count this design specifies is well-defined on every row that
exists.

**Intake today** (§1b, §2): ~83 new assignments in the last 7 days, 348 in the last 30, spread over 25
and 32 reviewers respectively. The trailing-7-day distribution is skewed:

| trailing 7d | 14 | 7 | 6 | 5 | 4 | 3 | 2 | 1 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| reviewers | 1 | 1 | 2 | 3 | 1 | 4 | 6 | 7 |

Median 2, mean 3.3, top 14. Over 30 days the busiest reviewers sustain 5–7/week.

**The premise, quantified** (§5b) — `maximum_capacity` does not bound flow for anyone who clears
quickly:

| `maximum_capacity` | new PRs in 30 days | implied /week |
| --- | --- | --- |
| 10 | 30 | 7.0 |
| 20 | 29 | 6.8 |
| 10 | 23 | 5.4 |
| 10 | 22 | 5.1 |
| 20 | 22 (14 of them in the last week alone) | 5.1 |

**What a limit would cost** (§3b, §3c, §4). Peak rolling 7-day intake per reviewer — how big each
reviewer's worst week has been — over the whole history, and over the 32 reviewers still active in the
last 30 days:

| population | reviewers | p50 peak | p90 peak | max | blocked at 3 | at 5 | at 8 | at 10 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| all history | 41 | 6 | 12 | 16 | 36 | 26 | 15 | 7 |
| active in the last 30 days | 32 | **5** | 10.9 | 14 | 25 | **13** | 7 | 4 |

The second row is the one to design against: the all-history figures are inflated by the apply
pipeline's own June–July rollout, and the correction is not cosmetic — at a 5/week limit it halves the
affected population, 26 of 41 down to 13 of 32.

Replaying the last 90 days as if every reviewer had a limit:

| limit /week | assignments withheld | share of intake | reviewers who would hit it |
| --- | --- | --- | --- |
| 2 | 573 | 68.3% | 38 |
| 3 | 425 | 50.7% | 36 |
| 5 | 226 | 26.9% | 26 |
| 8 | 79 | 9.4% | 15 |
| 10 | 32 | 3.8% | 7 |
| 15 | 1 | 0.1% | 1 |

And the same replay over the last 30 days only (§4b), which drops the rollout period:

| limit /week | assignments withheld | share of intake | reviewers who would hit it |
| --- | --- | --- | --- |
| 2 | 255 | 73.3% | 29 |
| 3 | 185 | 53.2% | 25 |
| 5 | **103** | **29.6%** | 13 |
| 8 | 37 | 10.6% | 7 |
| 10 | 16 | 4.6% | 4 |
| 15 | 0 | 0.0% | 0 |

**That is higher, not lower, and the reason matters.** The natural reading of §3c — half as many
reviewers blocked once the rollout drops out — is that the limit would cost less in steady state. The
opposite is true: at 5/week the recent 30 days would have withheld **29.6%** of intake against the
90-day view's 26.9%. §3c's halving was a *population* effect (nine stale reviewers leaving the
denominator, carrying their July peaks with them), not a fall in intensity. Intake has become **more
concentrated**, not less: the 13 largest recipients took 222 of the last 348 assignments (64%), so
fewer reviewers hit a 5/week cap while those who do account for a larger share of the flow. (The
probe does not join "blocked" to "largest recipient", so the two sets of 13 are near-certainly but not
provably identical.)

The redistribution arithmetic survives that, at least in aggregate: withholding 103 assignments over
30 days is ~24/week, against ~66/week of unused headroom among the 19 active reviewers a 5/week cap
would *not* touch (they currently receive ~29 of the ~81 assignments/week). Whether those particular
PRs match those particular reviewers' topics is still the open question, and still needs an engine
simulation rather than history.

§3b (peak distribution) and §4 (90-day replay) are independent computations, and on the live data they
agree **exactly** on the affected-reviewer count at every limit — 36 / 26 / 15 / 7 — which is the best
internal check the probe offers. Getting them to agree exposed an off-by-one worth stating, because
the implementation has to get it right too: the gate lets a reviewer *reach* their limit
(`recent + simulated < limit`), so a reviewer whose worst week was exactly N is never blocked at N.
§3b originally counted `peak >= N` and disagreed with §4; it now counts `peak > N` (columns renamed
`would_block_at_N`) and the two line up. The 30-day pair reproduces it independently: §3c and §4b
agree at 25 / 13 / 7 / 4.

That table is easy to over-read, so three things to hold onto:

- These are **withholdings from a reviewer, not PRs left unassigned.** When the gate removes a
  reviewer, the engine offers the PR to the next eligible candidate; whether one exists is a
  topic-matching question this history cannot answer. Sizing *that* needs an engine simulation over a
  snapshot, not this table. What can be said from the supply side: 37 reviewers have `auto_assign` on,
  so a universal 5/week cap would still allow ~185 assignments/week against the ~83/week actually
  being made — aggregate headroom is not the binding constraint; topic match might be.
- The counts are an **upper bound** (a blocked assignment would also have lowered later windows), and
  they assume **universal adoption**, which is explicitly not the plan.
- §4's replay spans the entire history (younger than 90 days), rollout included — but the obvious
  inference, that it therefore overstates steady-state cost, is wrong. §4b measured 29.6% against
  §4's 26.9%; see the reversal above. Fewer reviewers affected does not mean less intake withheld.

**So `≤5/7d` — this doc's running illustration — is well chosen, and for a sharper reason than the
doc originally had.** Five is exactly the median active reviewer's *worst* week: the median reviewer
never trips it, while it binds for the 13 of 32 whose peak runs higher — withholding 29.6% of the
last 30 days' intake, if all 13 had it set. "At most five new PRs a week"
is, empirically, "never take more than a typical colleague's busiest week" — which is a defensible
thing for a knob to mean. That is the right size for something a reviewer opts into deliberately and
the wrong size for a global default (Open Question 3). A limit of 10+ is close to decorative: only 4
of the 32 active reviewers have crossed it in any rolling week.

### What the measurement changed

1. **Login case is a live risk, and the failure mode is worse than an undercount** (§6, §6d).
   **11 of the 41 reviewers** — 230 of 839 rows — are stored under a capitalized login:
   `build_reviewer_catalog` copies `User.github_login` in its original case
   (`reviewer_assignment.py:243-252`), the engine appends it verbatim
   (`reviewer_assignment_engine.py:172`), and `assign_reviewer_and_record` writes it unchanged. No
   login appears under *two* spellings (41 raw = 41 normalized) and every one resolves to a
   `core_user`, so a service filtering `reviewer_login__in=<normalized logins>` would not partially
   undercount those 11 — it returns **zero** for them, and their weekly gate silently never fires. A
   quarter of the reviewer population would appear to have opted into a limit that does nothing.
   `lower()` on both sides is load-bearing; its unit test is not optional.
2. **Distinct-PR counting is insurance, not a live correction** (§7). Zero churn in 67 days: 839
   applied rows are 839 distinct `(PR, reviewer)` pairs, at most one row each. Keep the rule — the
   attention sweep can produce a repeat and row-counting would then be wrong — but nothing in
   production currently depends on it. (The 600-distinct-PRs-to-839-pairs gap is PRs collecting ~1.4
   *different* reviewers, which the distinct rule was never about.)
3. **Single-run clustering is mild** (§9), which is the empirical backing for Subtlety 6. Over the
   last three weeks the push delivers 6–21 PRs a night across 4–15 reviewers — 1.0–1.8 per reviewer
   per night, recent maximum 4. Every 7–10-per-night case in the history falls in 2026-06-25 →
   2026-07-30, the apply pipeline's own rollout. A nightly drip of 1–2 is already the norm, so the
   weekly window needs no intra-week pacing parameter.
4. **Confirm-mode over-proposal is a small-population risk** (§5). 6 of 57 preference rows are
   `confirm`, and the three that received anything in 30 days got 8, 3 and 1 PRs. Watch it in the
   pilot as planned; it is not shaping v1.
5. **No `053` pull-claims exist yet** (§8). All 348 applied rows in the last 30 days are
   snapshot-anchored; zero have a NULL `snapshot_id`. `053` went live 2026-08-27, one day before this
   run, so that is the absence of a signal, not evidence — the proxy itself works. Open Question 4
   stays open and should be re-measured once the claim path has real usage.
6. **Reviewers cannot pick a number they cannot see.** Median intake is 2/week while the median *peak*
   week is 6; a reviewer setting "max new assignments per week" without those numbers is guessing.
   Folded into [Surfacing](#surfacing--extend-the-honest-load-line).

§3c and §6d were added after the first run to answer exactly these two questions and were run the same
day; **§4b**, the 90-day replay restricted to the last 30 days, followed in a third run. All three are
folded in above — and §4b refuted the prediction this doc had been carrying, which is why the cost
figures moved *up* rather than down once the rollout period was excluded.

## Goals / Non-Goals

**Goals**
- Add a per-reviewer, per-repository **rolling weekly cap on new assignments** to the push pipeline,
  additive to (not a replacement for) `maximum_capacity`.
- **One legible knob**: a reviewer sets a single number, "max new assignments per week."
- **Opt-in**: no behavior change for any reviewer until they set a limit.
- **Smoothed by construction**: because the window is a week, a reviewer cannot burn a long-horizon
  budget in one burst (Bryan's concern) — no separate drip/pacing parameter required.
- Reuse the existing `ReviewerAssignmentApplication` history — no new log table.
- Report the weekly figure on the same surfaces as the concurrent load, so a reviewer can see why the
  push went quiet.

**Non-Goals**
- No change to the pull side (`053`) beyond overriding this limit like the other push throttles.
  Catch-up/burst is `053`'s job, not the push pipeline's.
- No change to `maximum_capacity` semantics, the fractional awaiting-author weight, or the assignment
  ranking/engine ordering (`037`).
- No separate "drip" / per-cycle pacing parameter, and no second (e.g. monthly) window — the single
  weekly window is the whole mechanism.
- No new history/audit model. No `PRTimelineEvent` reader for this feature (see Alternatives).

## Proposed Design

### Data model — one new opt-in field

`core.ReviewerPreference` (`qb_site/core/models/reviewer_preference.py`), alongside
`maximum_capacity`:

```python
# None = unlimited (no weekly limit); a positive value caps new assignments per rolling week.
max_new_assignments_per_week = models.PositiveIntegerField(null=True, blank=True)
```

Editable through the same three surfaces `maximum_capacity` uses — the console preferences form
(`core/forms.py`, `REVIEWER_PREFERENCE_EDITABLE_FIELDS` at `:27`, with a `clean_*` mirroring
`clean_maximum_capacity` at `:211-215`; blank ⇒ `None`), the Django admin, and the
`reviewer-topics.json` importer (`core/services/reviewer_topics_importer.py`).

### Counting the window — `ReviewerAssignmentApplication`, distinct PRs

The trailing-window count for `(repository, reviewer_login)` is:

> the number of **distinct `pr_number`** with an `applied` `ReviewerAssignmentApplication` whose
> `applied_at >= now − ANALYZER_ASSIGNMENT_RATE_WINDOW_DAYS`.

- **Distinct PR, not row count**: a re-cycled PR (auto-unassigned by the attention sweep, then
  re-assigned) writes multiple `applied` rows; the limit counts "new PRs", so a PR counts once.
- **Rolling window (default 7 days)**: a true rolling week, not a fixed Mon–Sun bucket, so there is no
  week-boundary cliff where everyone's budget resets at once. `ANALYZER_ASSIGNMENT_RATE_WINDOW_DAYS`
  *defines* what "per week" means; see the note on it in [Operational Notes](#operational-notes).
- **What this counts, precisely**: all *system-mediated* intake — nightly auto-assign, confirm-mode
  accepts, and console pull-claims (all write `applied` rows). It does **not** count a raw Zulip
  `assign` self-assign (no row is written). See [Subtleties](#subtleties--invariants) for why that is
  acceptable, and the one inconsistency it leaves.

A single service in `qb_site/analyzer/services/` owns this query:

```python
def recent_assignment_counts(
    repository: Repository, logins: Sequence[str], *, window_days: int, now: datetime,
) -> dict[str, int]: ...   # normalized login -> distinct applied PR count in the window
```

One grouped query over the model's `(repository, pr_number, reviewer_login, status)` index; pure and
unit-testable. Reused by both the engine integration and the load-line surfacing so the two cannot
disagree.

### The gate — a second condition, in the same place

Extend the engine's `ReviewerProfile` (`reviewer_assignment_engine.py:15`) with the rate context
(plain data, no ORM — consistent with `037`'s engine/integration split):

- `weekly_limit: int | None`               # `max_new_assignments_per_week`, or None
- `recent_assignment_count: int`           # the window count above

and extend the candidate gate in `_reviewer_candidate_state` so a reviewer is available only when
**both** hold:

```
remaining_concurrent > 0                                            # existing (stock)
AND (weekly_limit is None
     OR recent_assignment_count + simulated_this_run < weekly_limit)   # new (flow)
```

Read the strict `<` carefully: a reviewer with `max_new_assignments_per_week = 5` and four PRs in the
window is still available and receives a fifth; the *sixth* is what the gate blocks. "Max 5 per week"
means at most 5, not at most 4 — the probe's own §3b/§4 cross-check tripped on exactly this
([Measured Baseline](#measured-baseline-2026-08-28)), so the unit test should pin the boundary.

`simulated_this_run` is the per-reviewer count of assignments the engine has already handed out in
**this** run. The batch loop `run_assignment_simulation` (`:490-582`) already increments a reviewer's
weight on each pick (`:573-577`); increment a parallel `simulated_this_run` counter in the same spot
so a single nightly run cannot exceed the weekly cap. This is a correctness guard, **not** a pacing
knob: without it, the run's DB count wouldn't yet reflect this-run picks and the engine could overrun
the limit. `weekly_limit is None` short-circuits to today's behavior.

That is the entire mechanism. There is no separate drip: the weekly window *is* the smoother. A single
run may fill up to a reviewer's remaining weekly budget (bounded also by remaining concurrent
capacity), which is bounded, predictable, and within the reviewer's own stated weekly tolerance — see
Subtlety 6.

### Worked comparison

Nightly run; reviewer who reviews everything the same day, `maximum_capacity=10`:

| regime | night 1 | steady state | one week in | failure mode |
| --- | --- | --- | --- | --- |
| today (`maximum_capacity` only) | up to 10 | refilled to 10 nightly | 22–30 / month, measured | unbounded intake |
| monthly cap (`≤30/30d`) | up to 10 | up to 10 | **30 burned, then silent 3 wks** | intra-month burst |
| **weekly rate (`≤5/7d`)** | up to 5 | ≤ 5 in any rolling week | ≤ 5, steady | none — smooth by construction |

The bottom row is the target: the limit binds regardless of clear-rate, and it binds *smoothly*, from
one number.

### Surfacing — extend the honest load line

The concurrent load line (`reviewer_load.format_load_line`, shown by the `assigned-prs` Zulip command,
the daily attention DM, and the console) gains the weekly figure, e.g.:

```
Load: 3 / 10 (7 free) · last 7 days: 4 / 5
Load: 3 / 10 (7 free) · last 7 days: 5 / 5 ⚠ weekly limit reached
```

Computed from the same `recent_assignment_counts` service so the parts agree. This is load-bearing UX,
not decoration (same lesson as `053`'s Invariant 7): when the push goes quiet because the weekly limit
is hit, the reviewer needs to see *why*, and the line is where they see it — along with the implicit
nudge that `suggest-prs` will still serve them if they want more now. The second line above is the
state this feature exists to create and the one that most needs explaining: plenty of concurrent room,
no new work arriving. A reviewer with no limit gets the old line back byte-for-byte.

The copy says **"last 7 days", not "this week"** (an earlier draft of this section said the latter),
and the day count is read from `ANALYZER_ASSIGNMENT_RATE_WINDOW_DAYS` rather than written out, so it
cannot drift from the window actually being enforced. The window is rolling; "this week" invites the
calendar reading and, with it, a "why am I blocked, it's Monday" bug report.

The same number belongs **next to the field in the console preferences form**, before a limit is set.
Measured intake is a median of 2/week against a median peak week of 6
([Measured Baseline](#measured-baseline-2026-08-28)), so a reviewer choosing a number blind will pick
badly in either direction. Showing "You have been assigned N new PRs in the last 7 days" beside the
input costs nothing extra — the load line already computes exactly that count.

One thing rendering the page revealed that the design did not anticipate: the new field lands beside
`maximum_capacity` in the form's two-column grid — the right cell, stock next to flow — but
`maximum_capacity` has **never had help text**. An annotated field beside a bare one reads as an
oversight, and nothing on the page said these were two different *kinds* of limit. So
`maximum_capacity` gained a one-line explanation naming it as the concurrent hold. The stock/flow
distinction this doc is built on has to be legible on the surface where a reviewer sets both, not just
in the engine.

## Subtleties / Invariants

1. **Additive, orthogonal gate.** `maximum_capacity` (stock) and `max_new_assignments_per_week` (flow)
   are independent; a reviewer is auto-assignable only if under **both**. Neither replaces the other.
2. **Opt-in.** `weekly_limit is None` ⇒ the weekly gate is skipped entirely ⇒ byte-for-byte today's
   behavior. Nobody is affected until they set a limit.
3. **The limit throttles the push, and counts system-mediated intake.** Counting `applied`
   `ReviewerAssignmentApplication` rows makes the push gate count the push pipeline's own output plus
   the reviewer-initiated intake the system recorded (console pull-claims, confirm accepts). A raw
   Zulip `assign` self-assign is not recorded and does not count — defensible: it is the reviewer
   grabbing work entirely on their own, outside any pipeline the limit governs.
4. **One inconsistency, named.** Because console pull-claims write `applied` rows but Zulip
   pull-claims do not, a console pull counts toward the weekly limit while the identical action from
   Zulip does not. Inherited from `053`'s audit asymmetry, not introduced here. The clean fix is
   `053`'s deferred follow-up (route Zulip `assign`'s self-assign through `assign_reviewer_and_record`);
   until then the inconsistency is small and always in the reviewer's favour (Zulip pulls are "free").
5. **Confirm-mode reviewers: the limit counts *accepted* assignments.** A `confirm` reviewer's row is
   written on accept, not on propose (`050`). Pending proposals are already bounded by the concurrent
   cap (`add_pending_proposal_load`), and the weekly gate limits new *proposals* per week too (the
   engine's suggestions become proposals), so a confirm reviewer cannot be flooded. If over-proposal
   to slow-accepting reviewers shows up, add "active proposals created in the window" to the count —
   see [Open Questions](#open-questions). Out of scope for v1.
6. **A single run may fill the remaining weekly budget; that is acceptable, and deliberate.** With
   `≤5/7d` and an empty window, one night can assign up to 5 (further bounded by remaining concurrent
   capacity). This is bounded and within the reviewer's stated weekly tolerance. We deliberately do
   **not** add intra-week pacing — that was the "drip" the weekly window makes unnecessary. If
   single-night clustering ever proves undesirable, revisit; it is not a v1 concern. Measured
   (§9): the push currently delivers 1.0–1.8 PRs per reviewer per night, recent maximum 4, so the
   clustering this subtlety tolerates is not even occurring today.
7. **Default rule set only, matching apply.** Only the default rule set's snapshot is applied to
   GitHub (`046`), so only it writes `applied` rows. The window count and the gate therefore describe
   the same population the apply step acts on; per-ruleset compute-only variants neither write rows
   nor need the gate.
8. **Determinism / no new state.** The window count is a pure function of durable rows and `now`; the
   simulated counts live only for the duration of one run. No bucket state to persist, no drift (same
   philosophy as `050`/`053`).

## Interactions With Existing Pipelines

- **Pull side (`053`) must override this limit.** `053`'s `assignment_suggestions.py` substitutes an
  override profile (`maximum_capacity=sys.maxsize`, `auto_assign=True`, `temporary_break=False`) so an
  explicit request ignores every push throttle. `max_new_assignments_per_week` is a push throttle and
  must join that list — set the override profile's `weekly_limit` to `None`. **This is the one
  required edit to `053` code**, one field in the profile substitution, plus surfacing the weekly
  figure in `053`'s own honest load line. Rationale is identical to `053` Invariant 4: a reviewer
  *asking* for work is not a statement about how much the *scheduled* pipeline should send. This is
  also what makes "catch-up" work — the vacation-returner who wants more than a week's trickle pulls.
- **Attention sweep (`028`).** Unaffected. Auto-unassign still frees concurrent capacity; the freed
  PR's original `applied` row stays in the window, so churning a PR does not refund weekly budget —
  correct, since it was still "new work" that week.
- **Acceptance gate (`050`).** As in Subtlety 5, the limit counts accepted (applied) assignments;
  pending proposals are throttled by concurrent cap + weekly gate.
- **Legacy `src/queueboard/suggest_reviewer.py`.** Out of scope; the applied pipeline is the Django
  `analyzer` path (`046`). The legacy compute path is not gated here.

## Implementation Plan (Chunks)

Run `uv run ruff check .` and `uv run ruff format .` before every commit; canonical full run is
`bash scripts/repo_check_compose.sh` (mind the AGENTS.md pipe-into-`head`/`tail` trap — read the exit
status unpiped).

0. **Probe (no code, no deploy) — run 2026-08-28,**
   `heroku pg:psql -a queueboard-backend -f scripts/probe_054_rate_limit.sql`; see
   [Measured Baseline](#measured-baseline-2026-08-28). Re-run before the pilot picks numbers.
1. **Model + migration.** ✅ `ReviewerPreference.max_new_assignments_per_week` (nullable) +
   `core/migrations/0008_…` (generated on host). No backup-policy change (existing table). Admin
   `list_display`, `reviewer-topics.json` import/export, and the console preferences form/template.
2. **Count service.** ✅ `analyzer/services/assignment_rate_limit.py` —
   `recent_assignment_counts(...)` over `ReviewerAssignmentApplication`, plus
   `assignment_rate_window_days()` so no caller hardcodes 7. Unit tests: distinct-PR counting,
   window boundary, status filter, case normalization, repo scoping, empty/disabled window.
3. **Settings.** ✅ `ANALYZER_ASSIGNMENT_RATE_WINDOW_DAYS` (7) in `settings/base.py` **and**
   `.env.example`.
4. **Engine.** ✅ `ReviewerProfile` gained `weekly_limit` / `recent_assignment_count` /
   `simulated_this_run` (all safe-defaulted); `_within_rate_limit` is the new condition in
   `_reviewer_candidate_state`; `run_assignment_simulation` folds each pick back into the picked
   reviewer's profile beside the existing weight bump. Trace records `at_rate_limit`.
5. **Integration.** ✅ Counts and limits are injected in `build_reviewer_catalog` rather than at
   `prepare_assignment_inputs` — one grouped query per catalog build, which means the nightly
   builder, the trace, `053`, *and* the load line all read the identical figure by construction.
6. **`053` override.** ✅ `weekly_limit=None` joins the override profile; the weekly figure rides
   `053`'s existing load line. Tested: a reviewer at their limit gets nothing from the push and the
   full list on demand.
7. **Surfacing.** ✅ `ReviewerLoad` carries `weekly_count` / `weekly_limit` / `at_weekly_limit` and
   `format_load_line` appends `· last 7 days: N / M` (`⚠ weekly limit reached` when spent), so
   `assigned-prs`, the attention DM, the console and `053` all render it from the one place.
8. **Docs.** ✅ This doc; `qb_site/analyzer/AGENTS.md` service list.

## Pre-Implementation Notes (sharp edges)

Captured for whoever implements this — likely a fresh session that will read this doc but not the
design conversation behind it.

- **Login normalization is a correctness issue, and the answer is already known: the column is not
  normalized.** `assign_reviewer_and_record`
  (`qb_site/analyzer/services/reviewer_assignment_apply.py:121-129`) stores `reviewer_login`
  verbatim from its caller; the `_normalize_login` call next to it is only for comparing against
  GitHub's response. The callers disagree: the nightly apply/propose paths pass engine logins
  (normalized), while the console accept passes `proposal.reviewer_login` and `053`'s claim passes
  `User.github_login` — which preserves GitHub's original case, since `core_user` enforces only
  case-*insensitive* uniqueness (`MichaelStollBayreuth` is stored as written). So
  `recent_assignment_counts` **must** `lower()` on both sides — the query filter and the returned
  keys — or a rate-limited reviewer walks through the gate. Measured: **11 of 41 reviewers** (230 of
  839 rows) are stored capitalized, and no login has two spellings, so the failure is not a partial
  undercount but a **zero** count for a quarter of the population — their limits silently never fire.
  See [What the measurement changed](#what-the-measurement-changed).
- **The new `ReviewerProfile` fields must default safe** (`weekly_limit=None`,
  `recent_assignment_count=0`). `_reviewer_candidate_state` is **shared code**: the nightly builder and
  `053`'s `suggest_reviewer_for_pr_with_trace` both call it. `053` sets `weekly_limit=None` on its
  override profile (see Interactions), but every *other* construction site (tests, any direct caller)
  must also get a no-op default, or you change behavior you didn't intend. Add the fields with
  defaults; don't make them required positional args.
- **Add a trace skip reason (`at_rate_limit`).** The engine's diagnostic trace records a
  machine-readable reason per unassigned PR (`037`); a reviewer filtered by the weekly gate should
  surface as `at_rate_limit`, parallel to the existing capacity reason, so the persisted nightly trace
  and admin explain a quiet reviewer. `053`'s skip tally needs **no** new row — the requester's limit is
  always overridden, so it can never fire there (mirrors why `053` has no `at_capacity` row).
- **Confirm-mode over-proposal is the subtlest interaction to watch** (Subtlety 5, Open Q2). Pending
  proposals write no `applied` row, so they don't count; a slow-accepting `confirm` reviewer can
  accumulate more pending proposals than their weekly number across nights. It self-limits at the
  *concurrent* cap (pending proposals consume concurrent load), so it is bounded — but validate it in
  the pilot before deciding whether to fold active-proposals-in-window into the count.
- **Surfacing threads a cheap count through ~4 call sites, and must not trigger a second payload read.**
  The weekly figure is a small indexed DB query, independent of the multi-MB snapshot payload
  `reviewer_load` already reads (`053` measured that read at ~411 ms). Compute the weekly count
  alongside — never by loading the payload again — and thread it into the load model so `assigned-prs`,
  the attention DM, the console, and `053`'s own load line render it consistently.
- **Perf and migration are non-issues.** `ReviewerAssignmentApplication` is tiny (859 rows on
  2026-08-28, ~12.5/day measured), so the distinct-PR count query needs no new index yet (revisit only
  if the table grows by orders of magnitude). The migration is a nullable-column add — fast, no backfill. Generate it on the host per
  the AGENTS.md note (the refused-DB-connection RuntimeWarning is harmless).
- **UI copy: "in any 7-day period", not "this week".** The window is rolling, not a calendar week; the
  reviewer-facing label and help text should say so, to avoid a "why am I blocked, it's Monday"
  confusion.

## Validation Plan

- **Unit (engine, pure):** limit blocks at `recent + simulated == weekly_limit`; a single run cannot
  overrun; `None` limit ⇒ unchanged suggestions; weekly gate composes with the concurrent gate
  (blocked if either fails).
- **Unit (count service):** distinct-PR semantics (a PR with two `applied` rows counts once); window
  boundary (`applied_at` just inside/outside); only `status='applied'` counts; login normalization.
- **Service (integration):** on a fixture snapshot + seeded `ReviewerAssignmentApplication` rows, a
  reviewer at their weekly limit receives no new suggestions though they have free concurrent capacity.
- **`053` regression:** the same rate-limited reviewer *does* get on-demand suggestions, and the load
  line shows `this week: N / limit`.
- **Surfacing:** `assigned-prs` / console render the weekly line; it matches the service.
- Canonical full run: `bash scripts/repo_check_compose.sh`.
- **Measure first (`053`-style) — done 2026-08-28.** `scripts/probe_054_rate_limit.sql`; results and
  their consequences in [Measured Baseline](#measured-baseline-2026-08-28). Re-run it before the
  pilot sets limits (§§2–3 move week to week) and after `053` has real usage (§8).
- **Manual, pre-enable:** run the assignment build for mathlib4 with a test reviewer limited low and
  confirm (a) the nightly suggestion set omits them once at the weekly limit, (b) intake stays under
  the limit across a fast-clearing week, (c) an on-demand `suggest-prs` still serves them.

### Measuring first (the probe)

`scripts/probe_054_rate_limit.sql` — read-only, **no dyno and no deploy**: `heroku pg:psql` runs the
file locally against the production database, so the probe does not have to ship first.

```bash
heroku pg:psql -a queueboard-backend -f scripts/probe_054_rate_limit.sql
```

Reviewer logins are pseudonymised (first 8 hex of `md5(lower(login))`) so the output can be pasted
into this doc. For real logins, change `\set show_logins 0` to `1` **in the file** — `heroku pg:psql`
does not forward psql flags like `-v` (the same gotcha `043` records). The script creates and drops
one temp view; everything else is a `SELECT`.

Nine numbered sections, each aimed at a question this doc currently answers from intuition (§1c is a
correctness check inside §1):

| § | question | what it decides |
| --- | --- | --- |
| 1 | is the count source alive? rows, distinct PRs/reviewers, date span, status mix | a short or thin history means §§3–4 cannot support a number yet |
| 1c | `applied` rows with `applied_at IS NULL` | must be 0 — the gate filters on `applied_at`, so any such row is invisible to it |
| 2 | trailing 7-day distinct-PR intake per reviewer | the headline: what "per week" is worth today |
| 3 | **peak** rolling 7-day intake per reviewer over all history, plus p50/p90/max | the limit-picking number — a cap below a reviewer's peak would have bound |
| 3c | the same peaks over reviewers active in the last 30 days | strips the apply pipeline's rollout period out of the distribution |
| 4 | what-if: assignments blocked at limits 2/3/5/8/10/15 over 90 days | how much a pilot limit actually withholds |
| 4b | the same replay over the last 30 days | the steady-state cost, which is *not* the same as §4's — see the baseline |
| 5 | reviewers with prefs vs reviewers who get intake; `maximum_capacity` beside weekly intake | whether the weekly gate or the concurrent cap binds first, per reviewer |
| 6 | login case hygiene, split spellings, applied logins with no `core_user` | the undercount in [Pre-Implementation Notes](#pre-implementation-notes-sharp-edges), measured |
| 6d | how many *reviewers*, not rows, are stored capitalized | the exact population a case-sensitive count would exempt from their own limit |
| 7 | distinct PRs vs rows (re-assignment churn) | what the distinct-PR rule actually saves over row counting |
| 8 | `snapshot_id IS NULL` (pull-claim) vs snapshot-anchored intake | [Open Question 4](#open-questions) with numbers instead of a shrug |
| 9 | per-day volume and worst single day per reviewer | Subtlety 6 — is single-night clustering already real? |

Two caveats on reading it. §4 is an **upper bound**: it replays history assuming every other
assignment still happened, but a blocked assignment would also have lowered later windows, so the
true number withheld is smaller. §8's provenance is a **proxy, not a recorded field**: `053`'s console
claim is the only caller that passes `snapshot=None` to `assign_reviewer_and_record`, and snapshots
are `update_or_create`d per `(repository, cache_key)` and never deleted, so a NULL `snapshot_id` is
stable rather than an artifact of `on_delete=SET_NULL`. Good enough to size Open Question 4; not the
provenance marker `053` deferred.

The script was validated against a seeded local Postgres 16 in both login modes (aggregates checked
against hand-built fixtures) and **run against production on 2026-08-28** —
[Measured Baseline](#measured-baseline-2026-08-28). §§3c and 6d were added afterwards, prompted by
that run, and §4b after those; all three were run against production the same day, so every figure in
the baseline is measured rather than projected.

## Operational Notes

- **Settings** (in `settings/base.py` + `.env.example`):

  | setting | default | purpose |
  | --- | --- | --- |
  | `ANALYZER_ASSIGNMENT_RATE_WINDOW_DAYS` | 7 | the period the per-week limit is measured over |

  This *defines* what "per week" means for the reviewer-facing field. Changing it silently changes the
  meaning of every reviewer's number, so it is an operational tuning knob, not something to move
  lightly. The limit itself is per-reviewer (`ReviewerPreference.max_new_assignments_per_week`), not a
  global setting. That table is the whole settings surface: there is **no** drip/pacing setting (the
  window is the smoother) and **no** enable flag (see below).
- **Rollout is inherently safe / opt-in, and ships without a feature flag** (decided 2026-08-27,
  Open Question 5 — unlike `046`/`050`/`053`): the code changes nothing until a reviewer (or an admin
  via bulk edit / importer) sets a limit, so the opt-in default *is* the off switch and clearing the
  pilot cohort's limits *is* the rollback. A small pilot cohort (a few reviewers who asked, e.g.
  Christian) validates the behavior before wider adoption.
- **No data migration / backfill.** The history is already there; the first rate-limited run simply
  reads the trailing window.
- **Surfacing must ship with enforcement**, not after — a reviewer whose push goes quiet needs the
  weekly line to explain it (Surfacing / Subtlety 8).

## Alternatives (discarded)

- **Monthly cap plus a derived per-cycle drip** (this doc's own earlier draft). Two coupled concepts —
  a 30-day ceiling and an invisible `ceil(cap/30)` per-night drip — where one rolling *weekly* window
  does the same work with a single reviewer-facing number and no opaque derived parameter. The weekly
  window is both the limit and the smoother.
- **Monthly cap plus an explicit weekly sub-cap** (two reviewer knobs; Christian's monthly budget with
  intra-month catch-up). Rejected: the second knob exists only to allow catch-up, and `053` already
  provides catch-up on demand — so the push side does not need to. One knob is enough.
- **A configurable window per reviewer** (let a reviewer pick "per 30 days" for a bursty monthly
  budget). Rejected for v1 as needless surface: the window is a global operational constant, and
  monthly-budget behavior is a niche the pull side covers. Easy to revisit if asked.
- **Count `PRTimelineEvent` (`type=ASSIGNED`) instead.** Complete (captures manual and Zulip
  self-assigns), and matches "new PRs assigned this week" literally. Rejected for v1: it needs a
  login→`User` bridge, churn/re-assign de-duplication, and a dependency on timeline-backfill
  completeness. `ReviewerAssignmentApplication` is already login-keyed (matching the engine's
  login-space), indexed, and scoped to exactly the intake the limit governs. Revisit if "total intake"
  (including raw Zulip grabs) is wanted — the same decision as `053`'s provenance follow-up.
- **Replace `maximum_capacity` with the weekly rate.** Rejected: the concurrent stock cap still does
  useful work (bounding how much a reviewer holds at once); the two limits answer different questions
  and compose cleanly.
- **Token-bucket / linear accrual.** Would smooth intra-week too, but needs bucket state or a more
  involved stateless reconstruction, to solve a within-week clustering problem that is bounded and not
  yet observed (Subtlety 6). Deferred as an upgrade, not a v1 need.
- **Count assignment events rather than distinct PRs.** Rejected: double-counts re-cycled PRs, which
  is not "new PRs".

## Open Questions

1. **Window length — decided: 7 days** (2026-08-27). 7 is the most legible ("per week") and smooths
   well; 14 would be gentler on reviewers with spiky areas at the cost of a longer burst horizon. 7 is
   the launch value; may revisit after the pilot, but it is not an open question for v1.
2. Should **active pending proposals** (confirm-mode) count toward the window, to bound over-proposal
   to slow-accepting reviewers (Subtlety 5)? Default: no, rely on concurrent cap + weekly gate.
3. Any **global default limit**, or leave it `None`/opt-in indefinitely? Recommendation firmed up by
   the measurement: **opt-in**, and do not set a global default on this evidence. A universal 5/week
   would have withheld 26.9% of the last 90 days' intake from the reviewer it was aimed at, touching
   26 of 41 reviewers — and **29.6%** over the last 30 days, touching 13 of the 32 still active (§3c,
   §4b). Note the direction: excluding the rollout period lowers the headcount but *raises* the intake
   share, because recent intake is more concentrated. Aggregate supply says that is absorbable (37 auto-assign reviewers × 5/week ≈
   185 vs ~83 actually assigned), but whether each withheld PR finds a *topic-eligible* alternate is
   exactly what the history cannot answer. A default needs an engine simulation over a snapshot
   first, not a pilot's say-so.
4. Do we want to later **exclude console pull-claims** from the count (so the limit is push-only)?
   Default: leave them counted — they are real intake. Measuring the question does *not* need `053`'s
   deferred provenance marker after all: the claim path is the only caller passing `snapshot=None`, so
   `snapshot_id IS NULL` already separates pull-claims from snapshot-anchored intake. That is an
   implementation detail rather than a declared field — fine to measure with, not something to build
   the gate on. As of 2026-08-28 it reads **zero** pull-claims in 30 days, which says nothing yet:
   `053` had been live for one day. Re-measure before deciding.
5. **Kill-switch flag — decided: none** (2026-08-27). No `ANALYZER_ASSIGNMENT_RATE_LIMIT_ENABLED`.
   `046`/`050`/`053` each shipped a master flag because each added behavior that *runs on its own* — a
   GitHub write sweep, a proposal pipeline, a new endpoint — and needed an off switch independent of
   per-reviewer state. This feature adds no such behavior: it is one extra condition on an existing
   gate, inert while every `max_new_assignments_per_week` is `NULL`, and turning it off for a reviewer
   is an edit to the one field that turned it on. A flag would buy a second off switch for something
   already off by default, at the cost of a permanent settings knob, an `.env.example` line, and a
   branch in the engine gate. If a pilot goes wrong, clear the pilot cohort's limits — same blast
   radius, no lasting surface.

## Related Decisions
- `037-reviewer-assignment-policy-simulation-and-priority-planning.md` — engine/integration split; the
  gate lives in the pure engine.
- `046-apply-reviewer-assignments-in-django.md` — `ReviewerAssignmentApplication`, the history source.
- `050-reviewer-assignment-acceptance-gate.md` — deferred this as "throughput cap"; confirm/propose
  interaction.
- `053-on-demand-assignment-suggestions.md` — the pull side that must override this limit and that
  provides catch-up/burst.
- `028-reviewer-queue-nudges-v1-daily-report.md` — attention sweep / auto-unassign interaction.

## Progress Notes
- **2026-08-27** — Draft written from the Zulip thread and a read of `037`/`046`/`050`/`053`. Settled
  before drafting: count source = `ReviewerAssignmentApplication` (distinct applied PRs); cap is
  opt-in (`null` default). Initial draft modelled a monthly cap + a derived per-cycle drip.
- **2026-08-27 (revised to Option A)** — reframed from "monthly cap + drip" to a **single rolling
  weekly rate** (`max_new_assignments_per_week`). Rationale: a raw per-cycle drip is not a legible
  reviewer knob, but the same smoothing expressed over a *week* is — and a weekly rolling window is
  simultaneously the throughput limit and the smoother, so it replaces both earlier parameters with one
  number. Catch-up/burst is delegated to the pull side (`053`), which already overrides push throttles.
  File renamed `054-monthly-assignment-cap.md` → `054-assignment-rate-limit.md`. Not yet implemented —
  awaiting review.
- **2026-08-27 (review)** — window length locked to 7 days (Open Question 1 closed). Added
  [Pre-Implementation Notes](#pre-implementation-notes-sharp-edges) capturing sharp edges for a fresh
  implementing session (login normalization, safe `ReviewerProfile` defaults given the shared engine
  gate, the `at_rate_limit` trace reason, confirm-mode over-proposal, cheap-count surfacing).
- **2026-08-27 (probe + no kill-switch)** — Open Question 5 closed: **no**
  `ANALYZER_ASSIGNMENT_RATE_LIMIT_ENABLED`, because the opt-in default already is the off switch
  (rationale recorded in the question and in [Operational Notes](#operational-notes)). Added
  `scripts/probe_054_rate_limit.sql` — the measure-first probe — plus
  [Measuring first](#measuring-first-the-probe) explaining what each section decides, and chunk 0 in
  the implementation plan. Validated against a seeded local Postgres 16 in both login modes; **not yet
  run against production**, so `≤5/7d` and every other figure here remain guesses. Writing the probe
  settled two code facts that are now folded back into the doc: `ReviewerAssignmentApplication`
  stores `reviewer_login` **verbatim, not normalized** (so the count service must `lower()` on both
  sides — Pre-Implementation Notes), and `snapshot_id IS NULL` is a usable stand-in for "came from a
  `053` pull-claim" (Open Question 4).
- **2026-08-28 (probe run against production)** — results and consequences in
  [Measured Baseline](#measured-baseline-2026-08-28). The premise is confirmed with numbers
  (`maximum_capacity=10` reviewers taking 22–30 new PRs a month), and `≤5/7d` turns out to be a real
  constraint rather than a token one — below the median reviewer's peak week. Six claims in this doc
  were re-sized against data: the login-case risk is live and fails *open* (a capitalized reviewer
  counts zero, so their gate never fires); distinct-PR counting is insurance with zero
  observed churn; single-night clustering is 1–2 PRs, not the burst Subtlety 6 tolerates;
  confirm-mode is 6 of 57 reviewers; `053` claims are not yet measurable; and reviewers need their
  own intake shown next to the field to pick a number at all. Open Question 3 firmed up to "no global
  default without an engine simulation". Probe gained §3c (peaks over active reviewers only) and §6d
  (capitalized reviewers, not rows).
- **2026-08-28 (second run: §3c, §6d)** — both sharpen the picture rather than change direction.
  Restricting peaks to the 32 reviewers active in the last 30 days drops the median worst week from 6
  to **5** and halves who a 5/week limit would block (26 of 41 → **13 of 32**) — so the all-history
  figures were indeed carrying the apply rollout, and `≤5/7d` lands exactly on the median active
  reviewer's worst week. §6d puts a number on the login-case bug: **11 of 41 reviewers** are stored
  capitalized, so a case-sensitive count would silently exempt a quarter of the population from their
  own limit. The corrected `peak > N` predicate was confirmed against §4 on live data (36/26/15/7,
  exact). Added §4b (30-day replay) to the probe; not yet run.
- **2026-08-28 (third run: §4b)** — and it refuted the prediction this doc was carrying. §4's 90-day
  replay was expected to *overstate* steady-state cost because it spans the apply rollout; the 30-day
  replay came back **higher**, 29.6% withheld at 5/week against 26.9%. §3c's halved headcount was a
  population effect (stale reviewers leaving the denominator), not falling intensity: recent intake is
  more concentrated, with the 13 largest recipients taking 64% of the last 348 assignments. The
  redistribution arithmetic still closes in aggregate (~24 withheld PRs/week against ~66/week of
  headroom among the 19 reviewers a 5/week cap would not touch), but only in aggregate — topic
  matching remains an engine-simulation question. Open Question 3 updated with both figures and the
  direction of the difference.
- **2026-08-28 (implemented)** — chunks 1–8 landed; the feature is inert until a reviewer sets a
  limit. Three places where the implementation is sharper than this doc had it, recorded because
  each was a real choice:
  - **The count is fetched in `build_reviewer_catalog`, not `prepare_assignment_inputs`.** The plan
    said the latter *via* the former; putting the query in the catalog builder means the load line
    gets the figure for free (it builds a catalog too), which turns "the gate and the surfacing must
    agree" from a discipline into a structural property. One grouped query per catalog build.
  - **`simulated_this_run` is a `ReviewerProfile` field, not a parallel dict.** A dict alongside
    `assignment_stats` would have matched the existing `_current_weight` pattern but had to be
    threaded through five signatures plus the `PRAssignmentPriorityScorer` type. Instead
    `run_assignment_simulation` keeps a local reviewer list and `replace()`s the picked reviewer's
    profile beside the existing weight bump — same spot, no signature churn, and the `recent` /
    `simulated` split the doc specifies stays visible in the data.
  - **The reviewer-facing copy says "last 7 days", not "this week"** (the Surfacing section's own
    example), following the Pre-Implementation note: the window is rolling, and the calendar reading
    is exactly the confusion that note predicted. The form label and help text derive the number
    from `ANALYZER_ASSIGNMENT_RATE_WINDOW_DAYS`, so the copy cannot drift from the mechanism.

  Two small decisions the doc did not cover: a limit of **0 is rejected** by the form (that is
  `auto_assign` off, which says so on every surface, rather than a rate only the engine gate could
  explain), and a **non-positive window counts nothing** rather than degenerating into "all of
  history", which would block every limited reviewer at once.

  Verified: the new suite (24 tests) plus `analyzer`, `console`, `core`, `zulip_bot` and `api`
  (935 + 152) all pass, along with `manage.py check`, `makemigrations --check`, backup-policy
  validation and GraphQL validation. `scripts/repo_check_compose.sh` was not run end-to-end; its
  steps were reproduced individually against the dockerized Postgres. The three
  `syncer.tests.tasks.test_commit_history_tasks` errors are the documented bare-host `GH_TOKEN`
  absence, unrelated to this change.
- **2026-08-28 (preferences page reviewed, two fixes)** — rendering `/console/preferences/` rather
  than reasoning about it caught something the design had not: the rate-limit field lands beside
  `maximum_capacity` in the two-column grid, which is the right cell, but `maximum_capacity` has no
  help text and the new field had 247 characters of it — a bare number beside a wall of prose, with
  nothing saying they are different *kinds* of limit. Fixed by giving `maximum_capacity` a one-line
  explanation and cutting the new field's text to 113 characters (168 with the measured-intake
  sentence) by dropping the half that restated its own label. Every other help text on the page is
  59–97 characters, so the pair now differs by about a line instead of four.
  [Surfacing](#surfacing--extend-the-honest-load-line) updated to the shipped copy.

  A second, unrelated grouping bug on the same page was fixed in its own commit and is recorded here
  only because it was found by this work: `stale_nudge_days` and `auto_unassign_days` are one
  escalation ladder with a cross-field rule (`Y > X`) but sat in different sections, so the
  validation error landed on a field whose partner was off screen. The obvious repair — moving
  auto-unassign under Notifications — would have been wrong: `needs_auto_unassign` is computed with
  no reference to the reviewer's `notifications_enabled` (only the global enforcement flag gates
  it), so filing it there would tell reviewers that switching notifications off stops them being
  unassigned. It does not. The pair fits neither section, which is why it got split, so it now has
  its own "Stale PRs" section and each half's help text names the switch that governs it. Nothing to
  do with 054's mechanism.
- **2026-08-28 (deployed)** — shipped to production. No settings change was required: the one new
  setting (`ANALYZER_ASSIGNMENT_RATE_WINDOW_DAYS`) defaults to 7 and there is no feature flag, so
  the deploy is a schema change plus inert code. The migration rides Heroku's release phase
  (`Procfile`), which is the one genuine deploy hazard worth naming — `build_reviewer_catalog`
  selects the new column, so an unapplied `core.0008` would take the nightly assignment run, the
  console, `assigned-prs` and `suggest-prs` down together rather than degrading quietly. A failed
  release phase aborts the deploy, so this is self-guarding.

  Behavior with every limit still `NULL` is unchanged in the ways that matter — the engine gate
  short-circuits, `format_load_line` returns its previous string byte-for-byte, the persisted trace
  drops the empty `at_rate_limit` key, and the `reviewer-topics.json` export omits an unset limit.
  Two things did change for everyone: the preferences page looks different (new field, new "Stale
  PRs" section, new help text), and `build_reviewer_catalog` now runs one extra grouped count query
  per call whether or not anyone has a limit — negligible against an 859-row table, but not zero.

  Next: set a low limit on a pilot reviewer, confirm the gate binds and the load line explains it,
  then re-measure §8 now that `053` has real usage before deciding Open Question 4.
