# On-Demand Assignment Suggestions (Reviewer-Initiated "What Should I Review?")

> Status: **Planned** (living implementation plan). No code written yet. All feature flags will
> default off, following the 046/050 staged-rollout discipline. The design below is calibrated
> against a production measurement taken 2026-08-25 — see [Measured Baseline](#measured-baseline).

## Context

- Reviewer assignment today is entirely **push-based and scheduled**: `analyzer.refresh_reviewer_assignments`
  computes a `{pr_number: login}` snapshot nightly, and `analyzer.propose_reviewer_assignments`
  (design doc 050) either direct-assigns or opens an `AssignmentProposal` for it. A reviewer who
  finishes their queue at 14:00 has no way to get more work until the next nightly run.
- The acceptance gate (050) made assignment *consensual* but not *reviewer-initiated*. A reviewer
  with capacity and time right now can only wait, or hunt the queueboard manually and use the
  existing `assign` Zulip command on whatever they find — with no help from the matching engine
  that already knows which PRs suit them.
- **Push cannot reach a third of the reviewer base at all.** 19 of 57 reviewers have `auto_assign`
  off and 3 are away (21 distinct, one being both), and the scheduled pipeline is by construction
  silent toward every one of them. They are not marginal users of this feature — they are its
  target population, and a pull surface is the only mechanism that can serve them. See
  [Measured Baseline](#measured-baseline).
- The matching logic to answer "what should **I** review?" already exists; it is just wired in the
  opposite direction. `analyzer.services.reviewer_assignment_engine` answers *PR → reviewer*
  (`_reviewer_candidate_state`, `suggest_reviewer_for_pr`), and `rank_prs_for_assignment` already
  orders the queue by assignment priority. Inverting it needs **no engine change**: rank the
  assignable PRs with the shared scorer, then walk that ranking keeping the PRs where the
  requesting reviewer lands in the engine's own `available` list. This was verified against the
  live engine over 29,412 (reviewer, PR) pairs with zero disagreements.
- Two reviewer-facing surfaces already exist and are the natural homes: the Zulip bot
  (`zulip_bot/commands/`) and the reviewer console (`qb_site/console/`, design doc 050).

## Measured Baseline

Measured 2026-08-25 against production (`leanprover-community/mathlib4`, default rule set `3`,
queue snapshot 8 minutes old). Reproduce with `scripts/probe_053_suggestions.py` (read-only; see
[Reproducing the measurement](#reproducing-the-measurement)). These numbers are what the design
below is calibrated against; **re-run the probe before enabling the flags** to confirm the shape
has not changed.

**The candidate pool is large, and the scheduled pipeline places almost none of it.**

| stage | PRs |
| --- | --- |
| queue | 746 |
| − has an active assignee | −169 |
| − assignment-forbidden label (`maintainer-merge`) | −49 |
| − active `AssignmentProposal` | −12 |
| **assignable pool** | **516** (463 carry a topic label) |
| placed by the last nightly engine run | 11 |

The gap is not a backlog against the apply cap (`ANALYZER_REVIEWER_ASSIGNMENT_APPLY_MAX_PER_REPO`
is 50 in production and the engine produced only 11), and it is not missing area coverage (every
topic label present in the pool has at least one interested reviewer). It is the supply side being
throttled shut: **29 of 57 reviewers are at capacity** (median remaining capacity −0.01 — reviewers
sit pinned exactly at their cap) and 21 are unavailable by preference. Those two groups overlap in
one person, so 49 of 57 reviewers are throttled one way or the other; of the remaining 8, five
already receive work from the nightly run and three are held back only by a narrow label set.

**Every reviewer is reachable, by exactly one override.** Running the algorithm below for all 57
reviewers, the population decomposes cleanly — the groups are disjoint, verified pairwise:

| blocker | reviewers | unlocked by |
| --- | --- | --- |
| none — the nightly run can already assign them | 5 | — |
| availability (`auto_assign=false` / `away_until`) | 20 | the request's availability override |
| capacity | 29 | the request's capacity override |
| narrow label set | 3 | an explicit label override |
| unreachable under any override | **0** | — |

**There is plenty to suggest.** With availability and capacity overridden and the reviewer's own
labels, the median reviewer has **69** eligible PRs; 45 of 57 have at least 10, and only 7 have
three or fewer. A broad label override yields the whole 463-PR labelled pool.

**Why reviewers are skipped**, over all 29,412 (reviewer, PR) pairs, with real preferences and no
overrides:

| reason | pairs | share |
| --- | --- | --- |
| `no_area_match` | 20,961 | 71.3% |
| `no_topic_label` | 3,021 | 10.3% |
| `at_capacity` | 2,357 | 8.0% |
| `auto_assign_off` | 1,588 | 5.4% |
| `outranked` | 757 | 2.6% |
| *eligible* | 354 | 1.2% |
| `away` | 156 | 0.5% |
| `authored` | 110 | 0.4% |
| `excluded` (opt-out / cooldown) | 108 | 0.4% |

Consequences that shaped the design:

- **`no_area_match` dominates at 71.3%.** The label override is the highest-value part of this
  feature, not a convenience — it is the difference between 0 and the whole pool for a reviewer
  whose interests have drifted from their stored `preferred_labels`.
- **`outranked` is worth its tally, for a sharper reason than "one label loses to two".** It is
  757 pairs, but the meaningful denominator is the 5,430 pairs where the reviewer matched an area
  at all — **13.9%**. It is also *concentrated*: the median pool PR carries exactly one topic label
  (mean 1.01), so at most 58 PRs (≤11% of the pool) can outrank anybody. On those few multi-label
  PRs the `max_score` contest eliminates most matching reviewers at once.
- **Label supply and demand are badly skewed** even where coverage exists: `t-combinatorics` has 76
  pool PRs against 4 interested reviewers (19:1), `t-order` 7:1, `t-ring-theory` 5:1, `t-algebra`
  4.5:1. The scarcity-first ranking is doing real work.
- **The request costs ~476 ms, and 83% of it is one payload read.** Measured separately with
  `scripts/probe_053_payload_cost.py` (5 repeats, warm connection):

  | phase | mean | share |
  | --- | --- | --- |
  | snapshot payload load | 411 ms | 82.9% |
  | `prepare_assignment_inputs` (DB) | 28 ms | 5.6% |
  | rank the pool | 24 ms | 4.8% |
  | walk the whole pool | 17 ms | 3.4% |
  | load line | 16 ms | 3.3% |
  | **end-to-end** | **495 ms** (median 476, range 340–687) | |

  The payload is 5.62 MB TOAST-compressed on disk and 13.18 MB as JSON text (2.34× compression).
  Standalone, the ORM read is 267 ms median, splitting roughly two-thirds Postgres fetch +
  decompress + wire (198 ms) and one-third `json.loads` (88 ms) — neither reducible at the call
  site. `.only("payload")` is worth nothing (267 ms vs 270 ms for the full row); do not bother.
  **All engine compute together is 85 ms**, so the algorithm is not the thing to optimise.
- **Memory is the constraint to watch, not latency.** One held payload is a 28.8 MB Python object
  graph (2.19× the JSON text — lower than a naive guess, because the document is dominated by long
  strings rather than deep structure), but loading it peaks at 81.6 MB transient with the raw
  buffer and parsed graph alive together, and process RSS rose 63.5 MB across a single load. Peak
  RSS for the whole probe run was 259 MB. Steady state is comfortable; the transient spike is what
  could bite if several console requests load payloads concurrently on a small dyno.

Context on the surrounding pipelines, from the same run: 789 `ReviewerAssignmentApplication` rows
all-time (327 in the last 30 days, ~12/day); proposals stand at 15 accepted / 60 declined / 26
expired, though only 6 reviewers are in `confirm` mode, so that ratio is 6 people's behaviour and
is *not* load-bearing evidence for this design; 1,746 active `ReviewerOptOut` rows, of which only
114 exclusions land on 76 PRs still in the pool.

## Goals / Non-Goals

**Goals**
- A reviewer can ask, on demand, for a fixed number of open PRs they could review — from Zulip or
  from the console.
- The suggestion may be scoped to an explicit **set of labels**, which *replaces* the reviewer's
  stored `preferred_labels` for that request (so a reviewer can ask for work in an area they do not
  normally take).
- An explicit request overrides the reviewer's own **push-throttle preferences** — `away_until`,
  `auto_assign=false`, and `maximum_capacity`. All three configure how much the *scheduled*
  pipeline sends; none of them is a statement about what the reviewer wants when they are the one
  asking.
- Claiming is one step from the suggestion: the existing `assign` command in Zulip, a button in the
  console.
- Suggestions come from the *same* candidate pool the nightly builder uses, so they can never offer
  something the scheduled run would refuse.

**Non-Goals**
- No new `AssignmentProposal` rows. An on-demand request is not routed through the acceptance gate
  — the reviewer is already asking, so a propose→accept round trip is pure friction (see
  Alternatives). Design doc 050 remains the mechanism for *system-initiated* assignment.
- No reservation/hold on a suggested PR (see Invariant 8).
- No changes to the nightly compute/propose pipeline, the engine, or `ReviewerPreference`.
- No new Zulip mutation command. Claiming from Zulip reuses `assign`.
- No persistence of any kind on the read path — a suggestion is computed and discarded (Invariant 1).

## Proposed Design

### One read-only service, two renderers

New module `qb_site/analyzer/services/assignment_suggestions.py`. It is the single authority for
"which open PRs could this reviewer take right now, and why not the rest". Both surfaces render its
output; neither re-derives eligibility.

```python
@dataclass(frozen=True)
class SuggestedPR:
    pr_number: int
    title: str
    url: str
    author_login: str
    topic_labels: list[str]
    matched_labels: list[str]        # intersection with the effective label set
    queue_age_seconds: float | None
    available_reviewer_count: int    # scarcity, from the ranking scorer's `details`
    load_weight: float               # what claiming it would add to the reviewer's load


@dataclass(frozen=True)
class SuggestionResult:
    repository_id: int
    reviewer_login: str              # normalized
    effective_labels: list[str]      # the override set, else the reviewer's preferred_labels
    label_override: bool
    unknown_labels: list[str]        # requested labels that are not topic labels in this repo
    load: ReviewerLoad | None        # from the reviewer's REAL capacity — never the override
    suggestions: list[SuggestedPR]
    skipped: dict[str, int]          # reason -> count over the assignable pool
    snapshot_generated_at: datetime | None
    status: str                      # ok | no_snapshot | not_a_reviewer | no_labels | none_eligible


def suggest_prs_for_reviewer(
    repository: Repository,
    reviewer_login: str,
    *,
    labels: Sequence[str] | None = None,
    limit: int | None = None,        # None -> ANALYZER_ASSIGNMENT_SUGGESTIONS_LIMIT
    now: datetime | None = None,
) -> SuggestionResult: ...
```

Algorithm:

1. **Read the cached queue snapshot** for the repo's default rule set. Like
   `analyzer.services.reviewer_load`, this service **never builds** a snapshot — no snapshot means
   `status="no_snapshot"` and no suggestions, never a fabricated empty list.
2. **Assemble the candidate pool** via `_prepare_assignment_inputs` (promoted to a public
   `prepare_assignment_inputs`). This is the load-bearing reuse: it already applies the
   active-assignee filter, the assignment-forbidden label filter, the active-proposal exclusion, the
   per-PR opt-out and expired-proposal-cooldown exclusions, and folds pending-proposal load into
   reviewer weights. Suggestions therefore share one pool with the nightly builder by construction.
3. **Build the request profile.** Locate the requester's `ReviewerProfile` in `inputs.reviewers`
   (absent ⇒ `status="not_a_reviewer"`) and replace it, in place, with a `dataclasses.replace` copy:
   - `auto_assign=True`, `temporary_break=False` — an explicit request overrides availability;
   - `maximum_capacity=sys.maxsize` — an explicit request overrides the capacity throttle
     unconditionally (Invariant 7);
   - `preferred_labels` / `preferred_labels_lower` ← the requested label set, when one is given.

   Every downstream engine call then sees the override. **The engine itself is not modified**, and
   the substitution is the whole of the "explicit request" semantics — one seam, not a special case
   threaded through the engine.
4. **Compute the load line** from the *same* payload (`reviewer_load_for(..., snapshot_payload=payload)`).
   `build_reviewer_loads` rebuilds the catalog from `ReviewerPreference`, so it reports the
   reviewer's **real** `maximum_capacity`, not the `sys.maxsize` override — which is the point: the
   load line is now the *only* capacity signal the reviewer gets, and it must be true (Invariant 7).
5. **Rank** `inputs.assignable_queue_prs` with the shared `rank_prs_for_assignment` and the default
   scorer. The ranking is scarcity-first (fewest available reviewers, least remaining capacity),
   then queue age, then `feat:` priority, then PR number. That is the right order *among the PRs a
   given reviewer can take* — the PR that most needs this reviewer comes first. It is worth being
   honest that it is not a per-reviewer ordering: because scarcity-first front-loads PRs that are
   scarce precisely because few reviewers match them, the median reviewer's first eligible PR sits
   at rank ~78 of 516. Walking is cheap — 17 ms for the entire pool against a ~476 ms request — so
   this costs nothing, and the early-stop at `limit` is not an optimisation worth reasoning about:
   it can save at most 17 ms of a request dominated by the payload read.
6. **Walk the ranking**, and for each PR call `suggest_reviewer_for_pr_with_trace` with the override
   catalog. Keep the PR when the normalized requester login appears in `trace["available"]`;
   otherwise record a skip reason. Stop at `limit`. The trace's `picked` field is **ignored** — this
   service reads `available` / `potential` / `filtered` only, and never the random draw. This is
   what makes results reproducible (Invariant 2).
7. Return the top `limit` suggestions plus the skip tally.

### Skip tally ("why not more?")

Derived from the trace's existing `filtered` buckets plus a little local classification, counted
across the assignable pool. Reasons are attributed in the engine's own evaluation order, so each
pair is counted once against the *first* rule that excluded it:

| reason | meaning |
| --- | --- |
| `no_topic_label` | the PR carries no topic label at all (engine reason `missing-topic-label`) |
| `authored` | the requester is the PR author |
| `conflict_of_interest` | the requester lists the author as a conflict |
| `no_area_match` | none of the effective labels intersect the PR's topic labels |
| `outranked` | the requester matched, but another reviewer matched *more* labels, so the engine's `max_score` contest dropped them |
| `excluded` | active per-PR `ReviewerOptOut`, or expired-proposal cooldown |

There is deliberately **no `at_capacity` row**: the requester's capacity is always overridden, so it
can never be the reason *they* were skipped.

`outranked` is the non-obvious one and the reason this tally is worth building: without it, an empty
or short result looks like a bug. It fires on 13.9% of the pairs where a reviewer matched an area,
concentrated on the ≤11% of pool PRs that carry more than one topic label — see
[Measured Baseline](#measured-baseline).

### Zulip surface

New command `suggest-prs` (aliases `next-pr`, `suggest-pr`), registered like every other command in
`zulip_bot/commands/`.

```
suggest-prs [<owner/repo>] [<label> ...]
```

- No repo argument: one section per repository where the sender has a `ReviewerPreference`, matching
  how `assigned-prs` already behaves. With a repo argument: that repo only.
- Remaining tokens are the label override set (capped by `ANALYZER_ASSIGNMENT_SUGGESTIONS_MAX_LABELS`;
  unknown/non-topic labels are reported back rather than silently yielding nothing).
- **Reply in place** (`CommandResult(content=...)`), not a DM. The content is not sensitive, and —
  the deciding argument — the natural next step is `assign #12345`, which is itself an in-place
  command. Splitting the two halves of one flow across a DM and a channel would be worse than
  either. This follows `assign` / `console`, not `assigned-prs` / `prefs`.
- Shows `ANALYZER_ASSIGNMENT_SUGGESTIONS_ZULIP_LIMIT` (5), not the full service limit. Ten
  multi-line blocks would dominate a shared channel; the console link below carries the rest.
- Reply shape: the load line, then **one line per PR** (linked number + title + topic labels — the
  richer card with queue age and scarcity is the console's job), then a footer with:
  - the follow-up command, `assign #12345`;
  - `snapshot_generated_at`, because the reply is a permanent channel message that will still be
    sitting there tomorrow listing PRs that have since been claimed or merged, while the console
    page is always live;
  - a link to the console for more (see below).
- Still chunked through the shared `split_message_chunks` (`zulip_bot/services/zulip_client.py`).
  Five one-line entries land far inside Zulip's 10,000-character cap, so this is belt-and-braces
  rather than a live concern — the justification is the measured size, not the smallness of the limit.
- Empty result renders the skip tally as a one-line explanation.

**The console link.** Built with `build_site_url(reverse("console:suggestions"))` — a stable,
token-less URL, exactly like `commands/console.py`. No signed link is needed: design doc 052's
reasoning for why `close-pr` / `label-pr` require signed expiring tokens (they carry a target PR
plus a permission decision, for audiences wider than reviewers) does not apply, because suggestions
are reviewer-only and that is already the console's admission rule.

Carry the request shape in the query string — `?repo=<id>&labels=t-algebra,t-topology` — so that
"more suggestions" means more of *the same* question. Without it, a reviewer who asked for
`t-algebra` in Zulip lands on an unfiltered console page and the continuation silently becomes a
different query. The params are safe on a token-less URL because they pre-fill only repo and labels;
the login still comes from the session and never from the request (Invariant 6). Validate them
server-side regardless: `MAX_LABELS` applies to the query string too, and unknown labels already
have somewhere to go in `unknown_labels`.

**Footer wording must stay indefinite** — "more suggestions", never "the next 5" or "5 more like
these". The prefix property (Invariant 2) holds only within one snapshot generation, and
`ANALYZER_QUEUEBOARD_SNAPSHOT_PERIOD_SECONDS` is 300, so a reviewer who clicks through ten minutes
later may legitimately see a different set. In practice the head of the list moves slowly — the
dominant sort terms are `available_reviewer_count` and `total_remaining_capacity`, which change only
when loads change, while `queue_age_seconds` sits fourth and drifts monotonically for every PR at
once — but the copy must not promise what the refresh can break.

**Claiming from Zulip reuses the existing `assign` command.** It already self-assigns when no
`@**user**` is mentioned (`assignment_command_parser.py`), already refuses senders without a
`ReviewerPreference` for the repo (`missing_preference`), is already gated by
`ZULIP_ASSIGNMENT_MUTATIONS_ENABLED` and `ZULIP_COMMAND_POLICY`, and already enqueues a per-PR sync.
No new mutation surface, flag, or permission path is introduced on the Zulip side.

### Console surface

- **New page** `/console/suggestions/` (`console:suggestions`), reached from a "Find PRs to review"
  link on the home dashboard — including in the empty state, which currently offers a dead end
  ("no proposals, no assigned PRs"). A separate page, not a home section — but note the measurement
  weakens the *cost* half of that argument, so do not lean on it: the home already loads the same
  13 MB payload through `reviewer_load_with_breakdown`, so a home section reusing it would add only
  the ~85 ms engine run, where a separate page pays a fresh ~476 ms. The decision stands on the
  other two grounds: a label-override control plus a multi-select claim form is a page rather than a
  panel, and the engine should not run for every reviewer on every home render whether or not they
  came looking for work.
- GET renders the same `SuggestionResult` per repo at `ANALYZER_ASSIGNMENT_SUGGESTIONS_LIMIT` (10),
  with a label-override control. Accepts `?repo=` and `?labels=` to pre-fill the form from a Zulip
  footer link. Each suggestion carries an "Assign me" button; multiple may be selected.
- POST `console:claim` (`/console/suggestions/claim/`) takes `repo_id` + `pr_numbers[]` and, per PR,
  **re-runs `suggest_prs_for_reviewer` and verifies the number is still eligible** before writing
  (Invariant 6), then goes through the 046 mutation path `assign_reviewer_and_record` (GitHub assign
  + `ReviewerAssignmentApplication` + `sync_pr`). Partial failures are contained and rendered as an
  assigned/failed split, exactly like `console:unassign`'s `unassigned.html`.
- The login assigned is **always the session reviewer's own**, never taken from the request — the
  same invariant that makes `console:unassign` safe.
- Unassigning needs no new work: the home dashboard's assigned-PR roster is already a self-unassign
  form behind `ANALYZER_ASSIGNMENT_PROPOSALS_CONSOLE_UNASSIGN_ENABLED`.

## Subtleties / Invariants

1. **Suggestion is read-only and nothing is persisted.** No proposal rows, no GitHub calls, no
   snapshot builds, no `ReviewerPreference` writes, and no record that a suggestion was ever made.
   A suggestion is a pure function of (snapshot payload, live preference/proposal/opt-out state),
   computed and discarded. Safe to invoke from any surface, any number of times, by anyone entitled
   to their own suggestions. The cost of this choice is that assignment provenance cannot be
   reconstructed later — see Deferred Follow-Ups.
2. **Results are reproducible, and Zulip's list is a strict prefix of the console's.** Given one
   snapshot generation and unchanged live state, the same request returns the same PRs in the same
   order: the ranking sort key ends in `pr_number` so it is a total order with no ties; the service
   tests membership in `available` and ignores `picked`, so the weighted random draw never enters;
   and the catalog arrives through a stable sort over `order_by("user__github_login")`. Both
   surfaces walk the same ranking with different `limit`s, so the Zulip 5 are the first 5 of the
   console 10. This is what makes "more suggestions on the console" coherent — and it expires with
   the snapshot (300 s), which is why the copy stays indefinite.
3. **The request affects only the requester, and only toward more work.** That is precisely what
   makes overriding `away_until`, `auto_assign=false`, and `maximum_capacity` safe: a reviewer
   cannot use this to change anyone else's state, or to reduce their own obligations.
4. **Push-throttle preferences are overridable; correctness rules are not.** Overridable, because
   all three configure how much the *scheduled* pipeline sends: `away_until`, `auto_assign`,
   `maximum_capacity`. Never overridden: conflict-of-interest, per-PR `ReviewerOptOut`,
   expired-proposal cooldown, authorship, assignment-forbidden labels, PRs that already carry an
   assignee, and PRs held by an active `AssignmentProposal`. The first two are the reviewer's own
   standing decisions about *specific* people and PRs, not a statement about when or how much they
   are free.
5. **One candidate pool.** Suggestions must go through `prepare_assignment_inputs`. A second,
   hand-rolled filter chain here would drift from the builder and start offering PRs the nightly run
   refuses. New exclusion rules belong there, not at a call site.
6. **A posted PR number is re-verified against a fresh suggestion run before any GitHub write, and
   the login always comes from the session.** Without the re-check the console claim endpoint
   degrades into a general-purpose self-assign API that bypasses conflict-of-interest and opt-out
   rules. Because the request carries no eligibility-affecting intent (no `allow_over_capacity` to
   round-trip), the re-check is a pure re-evaluation of the same inputs: the only things that can
   legitimately make it disagree with the offer are real state changes.
7. **Capacity is reported, never enforced.** An explicit request overrides `maximum_capacity`
   unconditionally — asking *is* the consent, and requiring a second signal would have meant a
   dead end for 29 of 57 reviewers on the Zulip surface, which has no toggle to offer. The load
   line takes over as the honest capacity signal, so it is load-bearing UI rather than decoration:
   it must be rendered on both surfaces, and it must come from `reviewer_load_for` (real
   `ReviewerPreference.maximum_capacity`) and never from the overridden profile — otherwise it
   renders `Load: 10 / 9223372036854775807`. `format_load_line` already produces the honest
   `Load: 10 / 10 ⚠ at capacity`. The real second signal is the reviewer's decision to claim, taken
   with that line in front of them.
8. **No hold, and no pretence of one.** Two reviewers can be offered the same PR and both claim it.
   GitHub assignees are a set, so the outcome is two assignees — legal, visible, and self-correcting
   (the next nightly run drops an assigned PR from the pool). The claim result surfaces any
   co-assignee rather than implying exclusivity. Reserving a PR would mean an `AssignmentProposal`
   row as a mutex, which reintroduces the machinery this design deliberately avoids.
9. **The label override loses the `max_score` contest like anyone else — when it can.** Asking for
   `t-algebra` does not promote the requester above a reviewer who matches two of the PR's labels.
   Note the asymmetry this creates, and do not mistake it for a bug: a *broad* override cannot be
   outranked at all, because matching every label on a PR always ties `max_score`. The invariant
   therefore bites narrow requests only. Keeping the engine's contest honest is worth the occasional
   surprising empty result — which is exactly what the `outranked` tally exists to explain.
10. **Consistent numbers from one payload read.** The load line, the per-PR load contributions, and
    the eligibility decisions all derive from the same snapshot payload in one call, so the parts
    cannot disagree with the whole.

## Interactions With Existing Pipelines

- **Nightly builder:** a claimed PR gains an assignee, so `_filter_prs_without_active_assignee`
  drops it from the next run, and the claimer's higher load reduces what the engine gives them.
  No explicit coupling is required — the daily recompute absorbs on-demand activity the same way it
  absorbs manual assignment.
- **Over-capacity claims self-limit.** `_reviewer_candidate_state` requires `remaining > 0`, so a
  reviewer who claims their way to 13/10 receives nothing from the scheduled run until they drain
  back under their cap. This is the mechanism that makes removing the capacity gate safe: the
  reviewer can take on more, but only deliberately and one request at a time, and the push pipeline
  stops adding to the pile automatically.
- **Acceptance gate (050):** PRs with an active proposal are already excluded from the pool, so a
  suggestion can never collide with a pending proposal — including the requester's own, which is
  already presented to them on the console home. Pending-proposal load still contributes to the
  displayed load line via `add_pending_proposal_load`, but no longer gates what the requester is
  offered; that is intended, not an oversight.
- **Opt-outs:** the syncer clears an active `ReviewerOptOut` when a reviewer appears as an assignee
  (`pr_sync_service.py`, both the timeline-event and assignee-diff paths), so a claim self-heals
  stale opt-out state once the enqueued sync lands.
- **Stale-assignment backstop:** the 21-day auto-unassign sweep applies to claimed PRs unchanged.
- **Snapshot cadence:** `ANALYZER_QUEUEBOARD_SNAPSHOT_PERIOD_SECONDS` is 300, which sets the
  lifetime of a suggestion list (Invariant 2). Note also that the pool filters read *snapshot*
  assignees, so a PR claimed by someone else stays in the pool until the next snapshot lands.
- **Audit asymmetry (accepted, with a follow-up):** a console claim writes a
  `ReviewerAssignmentApplication` row via the 046 path; a Zulip claim through `assign` does not,
  because `zulip_bot/services/assignment_execution.py` calls `GitHubAssignmentClient` directly. This
  is pre-existing rather than introduced here, but this feature makes Zulip self-assignment routine,
  which raises its cost. See Deferred Follow-Ups.

## Implementation Plan (Chunks)

Each chunk is independently testable. Run `uv run ruff check .` and `uv run ruff format .` before
every commit; the canonical full run is `bash scripts/repo_check_compose.sh`.

1. **Service.** `analyzer/services/assignment_suggestions.py`; promote `_prepare_assignment_inputs`
   to a public `prepare_assignment_inputs`. This is a straight rename — the only three references
   are all inside `reviewer_assignment.py` itself (its definition plus
   `build_reviewer_assignment_trace` and `ReviewerAssignmentBuilder.build`), so no alias is needed.
   `scripts/probe_053_suggestions.py` imports the private name and must be updated in the same
   commit, or re-running the baseline will fail on import. Pure unit tests over fixture snapshot
   payloads.
2. **Settings.** All flags/tunables through `settings/base.py` **and** `.env.example` in the same
   commit (root AGENTS.md rule — this is the step most often forgotten, and skipping either half
   means the setting either has no effect in production or is undiscoverable for new deployments).
3. **Zulip command.** `commands/suggest_prs.py` + arg parsing + rendering; the console footer link
   with `?repo=`/`?labels=`; registry-dispatch coverage; `ZULIP_COMMAND_POLICY` note;
   `zulip_bot/AGENTS.md` update.
4. **Console.** `console:suggestions` + `console:claim`, templates, console-owned CSS built on the
   shared tokens, `?repo=`/`?labels=` pre-fill, home entry point (including the empty state); view
   tests mocking `assign_reviewer_and_record`; `console/AGENTS.md` update.
5. **Docs.** `analyzer/AGENTS.md` service list, this doc's finalization into ADR shape.

## Validation Plan

Unit tests (fixture payload, no GitHub/Zulip):
- ordering matches the scheduled ranking; `limit` respected;
- **prefix property**: the same request at `limit=5` returns exactly the first five of `limit=10`
  (Invariant 2) — this is the promise the Zulip footer's console link makes;
- **determinism**: two identical calls against the same payload return identical ordered results,
  proving the engine's random draw never leaks in through `picked`;
- label override replaces `preferred_labels`; unknown labels reported; `MAX_LABELS` enforced;
- `away_until`, `auto_assign=false` and `maximum_capacity` are all overridden; conflict, authorship,
  opt-out and cooldown are **not**;
- **the load line reports the reviewer's real capacity**, not the `sys.maxsize` override — an
  at-capacity reviewer gets both suggestions *and* `Load: 10 / 10 ⚠ at capacity` (Invariant 7);
- each skip-tally reason is produced by a case that only triggers that reason, `outranked` included;
  and no case ever produces an `at_capacity` skip for the requester;
- a broad label override is never `outranked` (Invariant 9);
- `no_snapshot` returns no suggestions rather than an empty-looking success.

Console view tests (existing `console/tests/test_views.py` patterns, session seeded, GitHub mocked):
- claim assigns via the 046 path and enqueues a sync;
- a posted PR number that is **not** in a fresh eligible set is rejected (Invariant 6);
- a claim for another reviewer's login is impossible (login comes from the session);
- `?repo=`/`?labels=` pre-fill the form and are validated, not trusted;
- partial failure renders the assigned/failed split.

Zulip tests: registry dispatch, the 5-item limit, footer contents (assign hint, snapshot timestamp,
console link carrying the requested labels), and the empty-result tally rendering.

Canonical full run stays `bash scripts/repo_check_compose.sh` (console is step `[12/12]`).

## Operational Notes

Settings (all `settings/base.py` + `.env.example`):

| setting | default | purpose |
| --- | --- | --- |
| `ANALYZER_ASSIGNMENT_SUGGESTIONS_ENABLED` | off | master switch for the read path on both surfaces |
| `ANALYZER_ASSIGNMENT_SUGGESTIONS_CONSOLE_CLAIM_ENABLED` | off | the console's GitHub write |
| `ANALYZER_ASSIGNMENT_SUGGESTIONS_LIMIT` | 10 | service default; what the console renders |
| `ANALYZER_ASSIGNMENT_SUGGESTIONS_ZULIP_LIMIT` | 5 | surface override for the in-channel reply |
| `ANALYZER_ASSIGNMENT_SUGGESTIONS_MAX_LABELS` | 5 | cap on the label override set (form and query string alike) |

`suggest_prs_for_reviewer(limit=None)` falls back to `ANALYZER_ASSIGNMENT_SUGGESTIONS_LIMIT`, so the
Zulip setting is a surface override rather than a second parallel knob.

- Zulip claiming needs no new flag: `assign` is already gated by `ZULIP_ASSIGNMENT_MUTATIONS_ENABLED`
  and `ZULIP_COMMAND_POLICY`.
- Console claiming needs the `assign_pr` operation token (`queueboard-assignment` app), already
  configured for the 046/050 paths.
- Rollout: re-run `scripts/probe_053_suggestions.py` to confirm the baseline still holds, enable the
  read path first on both surfaces (harmless — read-only), confirm the suggestions look sane against
  the queueboard, then enable the console claim flag.
- **Cost:** a request is ~476 ms, of which ~411 ms is the snapshot payload read and ~85 ms is all
  engine compute combined (see [Measured Baseline](#measured-baseline)). That is acceptable for a
  deliberate "find me work" page and is not a blocker, but it is slow enough to be worth fixing —
  and it means **the cache to build is not the one an earlier draft of this doc proposed**. Caching
  the *result* per `(repo, reviewer, label set, cache_key)` targets the cheap 17% and only helps a
  reviewer who repeats the identical request. The expensive 83% is repo-scoped and completely
  reviewer-independent: cache the **payload** per `(repo_id, cache_key, generated_at)` and every
  request in the repo is served from it, cutting the page to roughly its compute cost. Budget
  ~29 MB resident per cached payload per worker process, and note this is shared infrastructure
  rather than a 053 optimisation — `reviewer_load._latest_snapshot_payload` means the console home
  already pays the identical 411 ms today. Do not optimise the engine; there is nothing there.

### Reproducing the measurement

```bash
APP=<heroku-app>

# cheap pre-check, no dyno
heroku pg:psql -a "$APP" -f scripts/probe_053_precheck.sql

# eligibility probe (read-only; reviewer logins pseudonymised unless --no-anon)
PAYLOAD=$(gzip -9c scripts/probe_053_suggestions.py | base64 | tr -d '\n')
heroku run --no-tty --app "$APP" -- bash -c \
  "echo '$PAYLOAD' | base64 -d | gunzip > /tmp/probe.py && PYTHONPATH=\$PWD/qb_site:\$PWD python /tmp/probe.py"

# payload/latency probe (read-only)
PAYLOAD=$(gzip -9c scripts/probe_053_payload_cost.py | base64 | tr -d '\n')
heroku run --no-tty --app "$APP" -- bash -c \
  "echo '$PAYLOAD' | base64 -d | gunzip > /tmp/cost.py && PYTHONPATH=\$PWD/qb_site:\$PWD python /tmp/cost.py --repeats 5"
```

Each writes one JSON object between `===QB-PROBE-053-JSON-BEGIN===` / `===QB-PAYLOAD-COST-JSON-BEGIN===`
and the matching `...-END===` marker.

The eligibility probe's `A_as_is` pass cross-checks its own skip classifier against the live
`suggest_reviewer_for_pr_with_trace` on every (reviewer, PR) pair and reports `engine_mismatches` —
that count must be 0, or the classifier has drifted from the engine.

The cost probe times *queryset evaluation*, not attribute access: a `JSONField` is decoded when the
queryset is evaluated, so timing `snapshot.payload` afterwards measures nothing. An earlier draft of
this doc reported `payload_read_seconds: 0.0` for exactly that reason. If the dyno is OOM-killed,
re-run with `--size=standard-2x` — `tracemalloc` adds overhead at the moment the payload loads.

## Deferred Follow-Ups

- **Suggestion provenance, which requires the Zulip audit fix first.** These look like two
  independent follow-ups and are not. Answering "how many assignments came from a reviewer asking
  versus the nightly run?" needs a provenance marker on `ReviewerAssignmentApplication` — but
  Zulip's `assign` writes no such row at all, so a provenance field added alone would silently
  measure console claims only and read as "nobody uses Zulip for this". Routing `assign`'s
  **self**-assign through `assign_reviewer_and_record` is the prerequisite, not a sibling task.
  Shipping without either is a deliberate choice, not an oversight.
- **Cache the snapshot payload per `(repo_id, cache_key, generated_at)`.** The single highest-value
  performance change available, worth ~411 ms of a ~476 ms request, and it benefits the console home
  and `pr_info` as much as this feature. Deliberately out of scope here: it belongs to
  `analyzer.services.reviewer_load`, not to a caller.
- `why-not <PR url>` — a reviewer-facing single-PR explainer over the same trace machinery. Falls
  out of the skip tally almost for free once the service exists.
- `away` / `back` Zulip commands to set and clear `away_until` without opening the console — the
  natural companion to "I'm back, give me a PR" (today this feature deliberately *ignores* a stale
  `away_until` rather than fixing it).
- A user-supplied count, if the fixed limits prove wrong in practice.

## Alternatives (discarded)

- **Gate the capacity override behind an explicit second signal** (an `allow_over_capacity` flag, a
  console checkbox). Discarded on the measurement: 29 of 57 reviewers are at capacity, so the second
  signal would be needed by a majority of reviewers most of the time — a rubber stamp rather than a
  meaningful confirmation. Worse, the Zulip surface has nowhere to put such a toggle, so
  `suggest-prs` would have been a dead end for exactly those reviewers. `maximum_capacity` is a
  push-throttle like `auto_assign` and `away_until`, and belongs in the same bucket (Invariant 4);
  the load line carries the honest signal instead (Invariant 7).
- **Route on-demand requests through the acceptance gate** (create `AssignmentProposal` rows the
  reviewer then accepts on the console). Reuses 050 wholesale, but a reviewer who just asked for
  work does not need to be asked whether they want it, and in Zulip it is strictly *more* friction:
  the proposal still has to be answered in the console, so the round trip the console flow avoids
  comes back through the side door. "Park work for later" is already what the nightly propose
  pipeline does; it does not need an on-demand twin.
- **Asymmetric surfaces** (console assigns directly, Zulip creates proposals). Two meanings for one
  verb, and it pushes the Zulip user to a second surface to finish what they started.
- **A dedicated Zulip claim command** (`claim` / `take-pr`) routed through the shared service, for
  eligibility re-checking and 046 audit rows on the Zulip side too. Rejected for this iteration as
  duplicate surface: `assign` already does self-assignment with the right permission model. Revisit
  together with the provenance follow-up above.
- **Reserving a suggested PR** with a short-lived `AssignmentProposal` acting as a mutex. Solves a
  race whose worst outcome (two assignees on one PR) is legal, visible and self-correcting, at the
  cost of rows to create, expire and clean up on every failure path.
- **Modifying the engine to take a "requesting reviewer" parameter.** The override-profile
  substitution achieves the same thing with no change to a module that the nightly builder, the
  trace, and area stats all depend on.
- **One shared limit across both surfaces.** Ten one-line entries would still fit a Zulip message,
  but ten multi-line blocks dominate a shared channel, and truncating to a terser render alone would
  leave the Zulip user with no path to the remainder. Two limits plus a console link costs one extra
  setting and gives the reviewer somewhere to go.

## Progress Notes

- **2026-08-25** — Production measurement taken before writing any code (see
  [Measured Baseline](#measured-baseline)); probe committed as `scripts/probe_053_suggestions.py`
  and `scripts/probe_053_precheck.sql`. Four design changes followed from it:
  1. the capacity override became unconditional and
     `ANALYZER_ASSIGNMENT_SUGGESTIONS_ALLOW_OVER_CAPACITY` was dropped, along with the
     `allow_over_capacity` parameter, the console checkbox, the `at_capacity` status and its
     skip-tally row;
  2. the limit went 3 → 10, split into a console 10 and a Zulip 5 with a console link for the rest;
  3. the ranking justification in step 5 was softened, and Invariant 9 gained the broad-override
     asymmetry — both facts the measurement surfaced;
  4. provenance and the Zulip audit asymmetry were merged into one follow-up, because a provenance
     field alone would measure only half the claims.
  Also verified: the inverted-engine approach agrees with the live engine on all 29,412
  (reviewer, PR) pairs.
- **2026-08-25 (later)** — measured the one number the baseline had left open: the snapshot payload
  load, via `scripts/probe_053_payload_cost.py`. A request is ~476 ms and the payload read is 83% of
  it; all engine compute together is 85 ms. Two conclusions changed the doc. The cost note no longer
  says caching is premature — it is worth doing, but the cache must be keyed on
  `(repo_id, cache_key, generated_at)` around the *payload*, not on
  `(repo, reviewer, labels, cache_key)` around the *result*, because the expensive part is
  repo-scoped and reviewer-independent. And memory joined latency as a thing to watch: 28.8 MB
  resident per held payload, but an 81.6 MB transient peak while loading it.

## Related Decisions

- `020-reviewer-opt-outs-and-timeline-assignments.md`
- `022-zulip-prefs-form-design.md`
- `026-zulip-assign-unassign-and-github-app-tokens.md`
- `028-reviewer-queue-nudges-v1-daily-report.md`
- `037-reviewer-assignment-policy-simulation-and-priority-planning.md`
- `046-apply-reviewer-assignments-in-django.md`
- `050-reviewer-assignment-acceptance-gate.md`
- `052-session-authenticated-pr-action-pages.md` — why the console link here needs no signed token
