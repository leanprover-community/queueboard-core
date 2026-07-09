# Reviewer Assignment Acceptance Gate (Propose → Accept → Assign)

> Status: Living implementation plan (in progress — Chunk 1 landed). Captures decisions,
> invariants, and a chunked build plan; Progress Notes track what has shipped.

## Context

- Reviewers are auto-assigned to PRs, but assignments are frequently ignored/forgotten.
  A community request (private Zulip thread, Filippo Nuccio + Riccardo Brasca, with input
  from Dagur Asgeirsson, Rémy Degenne, Yaël Dillies, Jon Eugster, Michael Rothgang,
  Christian Merten) asks that an assignment be **proposed** to a reviewer, who must
  **accept** it within a few days before it is actually executed. The goal is that "an
  assignment really means the assignee knows they're in charge," reducing forgotten and
  doubled review effort.
- The existing reviewer pipeline is three decoupled daily Celery stages in `analyzer/`:
  - `analyzer.refresh_reviewer_assignments` (compute) — `ReviewerAssignmentBuilder`
    (`analyzer/services/reviewer_assignment.py`) + the pure engine
    (`analyzer/services/reviewer_assignment_engine.py`) produce
    `ReviewerAssignmentSnapshot.payload["automatic_assignments"]` = `{pr_number: login}`.
    Candidate pool = the `Queue` dashboard list minus already-assigned PRs
    (`_filter_prs_without_active_assignee`) and assignment-forbidden labels. Reviewer
    selection is topic-label-matched, author/COI-excluded, opt-out-excluded, and
    capacity-gated (weighted load vs `maximum_capacity`, default 10; see design doc 037).
  - `analyzer.apply_reviewer_assignments` (apply) — `apply_assignments_for_repo`
    (`analyzer/services/reviewer_assignment_apply.py`) re-validates each proposal against
    live data and POSTs the assignee via `GitHubAssignmentClient.assign(...)` +
    `assign_pr` App token, recording `ReviewerAssignmentApplication`. Gated by
    `ANALYZER_REVIEWER_ASSIGNMENT_APPLY_ENABLED` (see design doc 046).
  - `analyzer.reviewer_attention_daily` (attention) — nudges at `stale_nudge_days`
    (default 14) and auto-unassigns at `auto_unassign_days` (default 21, hard cap 21);
    per-cycle DM dedupe; queue-re-entry resets the clock (design doc 028).
- Reviewer config lives in `core.ReviewerPreference` (repo-scoped): `maximum_capacity`,
  `auto_assign`, `away_until`, `preferred_labels`, `conflict_of_interest`,
  `notifications_enabled`, `notification_settings` (JSON thresholds). Identity is
  `core.User` (`github_login` ↔ `zulip_user_id`).
- Reusable infrastructure already present:
  - Proactive Zulip DMs (`zulip_bot.services.zulip_client.ZulipClient.send_direct_message`).
  - GitHub OAuth for identity (`zulip_bot/services/github_oauth.py`,
    `views.register_github_callback`, `services/registration_linking.py`).
  - Per-PR opt-outs (`analyzer.ReviewerOptOut`, keyed `(repository, pr_number,
    reviewer_login)`), driven by timeline assign/unassign events
    (`syncer/services/pr_sync_service.py::_apply_assignment_opt_outs`).
  - GitHub App operation tokens (`core/services/github_operation_tokens.py`;
    operations `assign_pr` / `unassign_pr`).

### Key realization

With a **daily recompute loop already in place**, the pre-assignment gate needs almost no
bespoke per-PR sequencing. "One at a time, advance to the next reviewer on timeout" falls
out of *recompute + exclude*: an expired/declined reviewer is excluded next cycle, the
engine picks the next-best candidate, and a new proposal is created. No hand-written
state machine.

## Goals / Non-Goals

Goals:
- Insert an **acceptance gate** between compute and the GitHub assignment for reviewers in
  `confirm` mode: propose → notify → the reviewer accepts on a web console → *then* assign.
- Make the mode a **per-reviewer** preference, orthogonal to eligibility and notifications,
  with defined behavior for every combination and no disruption to existing reviewers.
- Provide a **reviewer console** (GitHub-login authed) to view and act on proposals and
  current assignments.
- Keep all reviewer contact on Zulip DM (and the console); **never** post to the GitHub PR.
- Surface the "awaiting acceptance" state on the **queueboard** so humans don't double-grab
  a PR mid-proposal, while the GitHub PR page stays clean.

Non-Goals (this phase):
- Email channel (no SMTP backend or `User.email` today — deferred; Zulip-first).
- The "re-confirm ~10 days after `awaiting-author` is removed" second prompt (Filippo's
  second proposal) — deferred to a later phase; it maps onto the attention sweep's
  queue-re-entry reset.
- Michael/Christian's "reviewed X PRs in 30 days → no more auto-assign" throughput cap —
  orthogonal; a separate capacity input to the engine.
- "Bot notices missed review comments" (Riccardo) — hard to detect reliably; out of scope.
- Changing the assignment *algorithm* (037) beyond feeding pending-load, pending-exclusion,
  and cooldown into the candidate filter.
- Unifying the proposal DM with the attention-sweep DM into a single per-reviewer digest
  (nice-to-have; later). Phase 1 only de-dupes the two so a reviewer never gets both a
  "newly assigned" ping and a "please accept" ping for the same event.
- Console-only `confirm` (a `confirmation_prompts_enabled` toggle, default true, to suppress
  even the proposal DM for a self-manager who checks the console manually). Deferred as
  YAGNI — a `confirm` reviewer with Zulip linked always gets the low-noise digest DM. Add
  only if a reviewer actually asks.

## Proposed Design

### Pipeline topology

```
refresh_reviewer_assignments  (unchanged)   COMPUTE snapshot {pr: login}
        │
   propose_reviewer_assignments  (NEW; replaces the direct-POST batch for confirm reviewers)
        │   per {pr, login}: branch on the reviewer's assignment_acceptance mode
        │     • auto     → existing 046 direct-assign path (GitHubAssignmentClient.assign)
        │     • confirm  → create AssignmentProposal(state=proposed,
        │                    expires_at = now + window)   [skip if already assigned /
        │                    PR already has an active proposal / opted-out / ineligible]
        │     • confirm but no reachable channel → fall back to auto (direct-assign)
        │
   digest DM per confirm-reviewer  → "N proposals expiring <date>, manage here: <console URL>"
        │
        ├── reviewer ACCEPTS on console → re-validate live → GitHubAssignmentClient.assign
        │        → proposal=accepted, record ReviewerAssignmentApplication, enqueue sync_pr
        ├── reviewer DECLINES on console → proposal=declined + active ReviewerOptOut (that PR)
        └── EXPIRES (silent)            → proposal=expired  (soft cooldown; see below)
        │
   next day: recompute excludes opted-out / cooled-down reviewer → engine picks the
             next-best candidate → propose to them.    ← "advance to next" for free
```

The current `apply_reviewer_assignments` logic **splits**: its batch half becomes the
`propose` step; its mutation half (`assign` + `ReviewerAssignmentApplication` + `sync_pr`
enqueue) moves into the **console accept handler**, triggered by a human. The 046
direct-assign path is **retained** and used verbatim for `auto`-mode reviewers.

### PR assignment state model (keep it comprehensible)

The proposal does **not** add a new orthogonal dimension; it adds **one intermediate value
to the existing assignment axis**. For an open, on-queue PR:

- **Unassigned** — no GitHub assignee, no active proposal.
- **Proposed** — no GitHub assignee, one active proposal to reviewer X (expires Y). *[NEW]*
- **Assigned** — has a GitHub assignee (accepted-via-gate, human-assigned, or `auto`-mode direct).

Invariants that keep this legible (design goal in its own right):

1. A PR is in **exactly one** of {unassigned, proposed, assigned} at any moment — enforced by
   the partial-unique index (one active proposal per PR).
2. **"Proposed" is a strict substate of "on-queue / awaiting review"** — *under the default
   `invalidate` on-queue-exit policy* (see below). With that default it cannot coexist with
   closed/merged/off-queue (those transitions invalidate the proposal), so it does *not*
   multiply against the full `PRStatus` enum. This invariant is policy-dependent: the
   `retain` policy deliberately relaxes it (a proposal may then persist off-queue), so
   load-accounting and surfacing read "active proposals" independent of queue state and must
   never *assume* proposed ⇒ on-queue.
3. The live state is always **reconstructable from durable facts** — GitHub assignees + the
   single active proposal. No derived state that can silently drift.
4. A **proposal is never an assignee.** Every surface renders "proposed to X" visibly
   distinct from "assigned to X"; the two are never conflated.
5. Terminal proposals (accepted/declined/expired/superseded) are **history, not live state**.
   They feed only the bounded expire-cooldown lookup; they never change "what state is this
   PR in now."

**On-queue-exit policy (resolved, but pluggable).** When a PR leaves the review queue before
acceptance (author pushes → `awaiting-author`, or the PR is closed), the default is to
**invalidate** the pending proposal (mark `superseded`/`expired`), preserving invariant #2.
This is intentionally kept open to future change, because opinions may differ:

- A single centralized predicate — `proposal_validity(pr, proposal, *, now)` in
  `analyzer/services/` — is the **sole authority** on "is this pending proposal still live,
  and if not, why." It is consulted by all three call sites that need a consistent answer:
  the expiry/reconcile sweep, sync-time reconciliation (when a human/self-assignee lands),
  and the console accept-time re-validation. No duplicated "still valid?" logic to drift.
- The behavior is selected by one setting, `ANALYZER_ASSIGNMENT_PROPOSAL_ON_QUEUE_EXIT` ∈
  `{invalidate (default), retain}`, read *inside* that predicate. Flipping to "let it ride"
  later is a one-setting change; the accept handler and sweeps already route through the same
  predicate, so no structural rework is required. Centralizing the predicate is good factoring
  regardless of the policy, so this extensibility adds negligible burden.

### New model: `analyzer.AssignmentProposal`

- `repository` FK → `core.Repository`
- `pr_number` (int)
- `reviewer_login` (str)  — matches `ReviewerOptOut` / snapshot login keying
- `snapshot` FK → `ReviewerAssignmentSnapshot` (`on_delete=SET_NULL`, provenance)
- `state`: `proposed` / `accepted` / `declined` / `expired` / `superseded`
- `proposed_at`, `expires_at`, `decided_at` (nullable), `notified_at` (nullable)
- `decided_via`: `console` / `auto_expire` / `sync_superseded` (and future `command`)
- `created_at` / `updated_at`
- **Partial unique index on `(repository, pr_number) WHERE state='proposed'`** — enforces
  at most one active proposal per PR (the "one at a time" invariant) race-safely at the DB
  level. Creation uses `get_or_create` / conflict-aware insert per the "Concurrent Writers
  and Unique Keys" rules in `qb_site/AGENTS.md` (never check-then-create).
- Index on `(repository, reviewer_login, state)` (plus `decided_at`) for the console query
  and the builder's cooldown lookup, keeping both index-served as history accumulates.

Reuses (no new models needed for these): `ReviewerAssignmentApplication` (accept audit),
`ReviewerOptOut` (decline enforcement), the DM machinery, the `assign_pr` token, and
`core.User` identity.

### New config field: `core.ReviewerPreference.assignment_acceptance`

- `CharField(choices=[("auto","auto"),("confirm","confirm")], default="confirm")`.
- **Data migration backfills all existing rows to `auto`** → grandfathered, zero
  disruption on day one.
- New rows inherit the `confirm` default → new reviewers opt into the gate by default.
- **Invariant:** every `ReviewerPreference` creation path must let the default apply, and
  `import_reviewer_topics` must set `assignment_acceptance` **only on create**, never in
  the `update_or_create` update `defaults`, or a re-import would flip existing reviewers.
- A bulk admin action / management command can set the mode across a selection of
  reviewers, preserving the ability to flip the whole pool later by community decision.

### Config axes and behavior matrix

Three orthogonal per-reviewer axes (for an eligible reviewer, `auto_assign=true`). Crucially,
`notifications_enabled` governs only the **optional attention nudges** (nudge / at-threshold /
newly-assigned FYI DMs). **Confirmation prompts are transactional and are NOT gated by it** —
they follow `assignment_acceptance=confirm`, because the prompt is the mechanism of the mode
you opted into. Choosing `confirm` *is* the opt-in to receiving proposal prompts; a reviewer
who wants true silence chooses `auto` (assigned directly, no clicks).

| `assignment_acceptance` | attention nudges (`notifications_enabled`) | behavior |
| --- | --- | --- |
| `auto` | on | Direct-assign on GitHub (046 path) + optional "you were assigned" DM. = today. |
| `auto` | off | Direct-assign, silently. = today. (Serves "notifications off but still auto-assigned".) |
| `confirm` | on | Propose → digest DM with console link → accept → assign, **plus** the optional nudges. |
| `confirm` | off | Propose → digest DM → accept → assign. Still prompted (proposals aren't gated by `notifications_enabled`); just no optional nudges. ← "opt into confirmations while opting out of other notifications." |

`auto_assign=false` → never auto-assigned; the gate is irrelevant. **Fall back to `auto`
only when the reviewer is genuinely unreachable** — no Zulip link (`core.User.zulip_user_id`
is null) — *not* merely because `notifications_enabled` is off. This decoupling matches doc
028's precedent that auto-unassign *enforcement* runs independently of `notifications_enabled`
(essential actions are not gated by the notification opt-in).

### Reviewer console

- **Auth:** GitHub OAuth → a persistent **reviewer session** (Django session framework;
  store the resolved `core.User`). Reuses `github_oauth.GitHubOAuthClient` and the existing
  callback plumbing. The DM contains a **plain, stable console URL** — no token, nothing to
  expire, bookmarkable. Standard login-redirect (`?next=`) lets a DM deep-link to a PR row.
- Console access is keyed on the authenticated `github_login` (which is exactly what
  proposals are keyed on), so console access and notification reach are independent: a
  reviewer can self-manage on the console even without a Zulip link.
- **Phase-1 view:**
  - *Pending proposals*: PR, matched topic labels, "why you", expiry countdown → **Accept**
    / **Decline**.
  - *Current assignments*: reuse the existing unassign capability (or read-only if we want
    Phase 1 minimal).
  - *Load summary*: "3 accepted + 2 pending / capacity 10" — makes "pending counts toward
    load" visible.
- **Accept handler:** re-validate live (PR still open, still unassigned, reviewer still
  eligible, not opted-out) → `GitHubAssignmentClient.assign` (`assign_pr` token) →
  `proposal=accepted` → record `ReviewerAssignmentApplication` → enqueue `syncer.sync_pr`.
- **Decline handler:** `proposal=declined` + upsert active `ReviewerOptOut(repository,
  pr_number, reviewer_login)`.
- Every POST **re-validates against live state**; a proposal that is no longer actionable
  (merged/closed/assigned-elsewhere/expired) renders a clear "no longer available, here's
  why" instead of an error.

### Decline vs. expire semantics

- **Decline** = explicit "not this PR" → permanent per-PR `ReviewerOptOut` (reuses existing
  enforcement; the builder already excludes opted-out reviewers).
- **Expire** (silent timeout) = soft. The proposal row stays `expired`; the builder applies
  a **cooldown**: skip a reviewer for a PR if they have an `expired` proposal for it within
  the last `PROPOSAL_EXPIRE_COOLDOWN_DAYS` (default ~14). *Not* a permanent opt-out — a
  reviewer who was simply away gets another shot later, while the PR still advances to
  other candidates now. This keeps `ReviewerOptOut` meaning "explicit no".

### Builder / engine integration

Three additions to the candidate/load computation (integration layer in
`reviewer_assignment.py`, seams in the engine kept pure/unit-testable):

1. **Candidate exclusion:** drop PRs that have an active (`proposed`) `AssignmentProposal`
   from the candidate pool (don't re-propose the same PR, don't propose it to a second
   reviewer). Extends `_filter_prs_without_active_assignee`.
2. **Load contribution:** a reviewer's active `proposed` proposals add to their weighted
   load (weight `PROPOSAL_PENDING_LOAD_WEIGHT`, default `1.0` — same as `AwaitingReview`; a
   proposal occupies a slot). Feeds `collect_assignment_statistics` / `build_reviewer_catalog`.
3. **Cooldown exclusion:** exclude a reviewer for a PR if they have a recent `expired`
   proposal (per the cooldown above).

### Surfacing: board + `pr-info` (required in Phase 1)

Because a `confirm`-mode PR has **no GitHub assignee during the pending window**, the
queueboard is the only place the state is visible. Both surfaces draw the assignment-axis
state from the same `AssignmentProposal` data:

- **Board:** the queue snapshot (`analyzer/services/queueboard_snapshot.py`) distinguishes
  *unassigned* / *proposed to X (expires …)* / *assigned* on the dashboard/API.
- **Single PR:** `analyzer.services.pr_info.get_pr_queue_info` → `PRQueueInfo` gains the same
  state, so the Zulip **`pr-info`** command and the API render it identically.

Nothing is ever written to the GitHub PR page.

**Surface history by audience** (see also the model's terminal states):
- Default views (board, `pr-info`) show only the **current** assignment state, optionally a
  compact "(N prior proposals)" hint — casual viewers stay uncluttered.
- **Full proposal history lives in Django admin** (the `AssignmentProposal` changelist,
  `ReadOnlyAdmin`) and an optional expanded `pr-info` mode — operators get the depth.
- Rationale: a visible "asked A (declined), asked B (expired), asked C (pending)" trail makes
  the effort to place a PR legible, which is the community's actual complaint ("did anyone
  get asked?"). Because history is append-only and never feeds live-state derivation (except
  the bounded cooldown), it cannot make "what state is this PR in?" ambiguous.

### History (retained, not expired)

The `AssignmentProposal` terminal rows *are* the proposal history (the *assignment* timeline
is already durable via `PRTimelineEvent` ASSIGNED/UNASSIGNED, `ReviewerAssignmentApplication`,
and `ReviewerOptOut`). **Keep proposal history indefinitely — no expiry/cleanup task**, at
least initially:

- Volume is low and append-only — on the order of tens of terminal rows per PR over its
  whole lifetime, bounded across the repo. Postgres handles this trivially; there is no
  storage pressure that would justify pruning.
- The history has standalone analytical value (accept rate, time-to-accept, decline/expire
  patterns per reviewer; inputs to future window/capacity tuning and the deferred throughput
  cap). Deleting it forecloses analyses we may want later.
- No correctness dependency on pruning: the expire-cooldown is a bounded *query window* over
  recent `expired` rows, not a deletion policy, so keeping older rows is harmless.

The model must therefore be classified for **backup retention** in `scripts/backup_policy.py`
(durable history, not a truncate-on-restore table). If volume ever surprises us, a cleanup
task can be added later without redesign — but it is explicitly out of Phase 1. Unifying
proposal history with the assignment timeline into one "assignment activity" view is likewise
a later step.

### Settings / flags (add to BOTH `settings/base.py` and `.env.example`)

- `ANALYZER_ASSIGNMENT_PROPOSALS_ENABLED` (bool; master kill switch for the gate)
- `ANALYZER_ASSIGNMENT_PROPOSALS_DELIVERY_ENABLED` (bool; DM delivery)
- `ANALYZER_ASSIGNMENT_PROPOSALS_ASSIGN_ON_ACCEPT_ENABLED` (bool; the GitHub mutation)
- `ANALYZER_ASSIGNMENT_PROPOSALS_DRY_RUN` (bool; log-only)
- `ANALYZER_ASSIGNMENT_PROPOSAL_WINDOW_DAYS` (default 7; per-reviewer overridable via
  `notification_settings`, honoring "≥7, weekly-aligned")
- `ANALYZER_ASSIGNMENT_PROPOSAL_EXPIRE_COOLDOWN_DAYS` (default 14)
- `ANALYZER_ASSIGNMENT_PROPOSAL_PENDING_LOAD_WEIGHT` (default 1.0)
- `ANALYZER_ASSIGNMENT_PROPOSAL_ON_QUEUE_EXIT` (`invalidate` default / `retain`; read inside
  the `proposal_validity` predicate — see the state model)
- Beat entries for `propose_reviewer_assignments` and the expiry sweep (clock-or-period,
  matching the reviewer-attention pattern). No cleanup task — proposal history is retained
  indefinitely (see "History"). Rollout follows the 028 discipline: *propose → deliver →
  assign-on-accept*, each independently toggleable, plus dry-run.

## Subtleties / Invariants

- The snapshot is advisory; **re-validate every assignment at accept time** (mirrors 046).
- The PR assignment state model (see above) is load-bearing for comprehensibility: exactly
  one of {unassigned, proposed, assigned}; a **proposal is never an assignee** on any
  surface; terminal proposals are history, not live state. Under the default `invalidate`
  policy "proposed" is strictly nested in "awaiting review"; the on-queue-exit behavior is
  centralized in the single `proposal_validity` predicate and selectable via
  `ANALYZER_ASSIGNMENT_PROPOSAL_ON_QUEUE_EXIT`, so a future flip to `retain` needs no rework.
- **At most one active proposal per PR** (DB partial-unique enforced).
- A pending proposal **counts toward load** and **excludes the PR from re-proposal**.
- **No GitHub write until accept**; **never** a GitHub PR-page comment or label for this
  feature.
- `auto`-mode reviewers use the **unchanged 046 path**; the gate is a per-reviewer branch,
  not a repo-wide replacement.
- **Confirmation prompts are transactional and are NOT gated by `notifications_enabled`**;
  that flag governs only the optional attention nudges. A `confirm` reviewer falls back to
  `auto` only when genuinely unreachable (no Zulip link at all) — never merely because
  nudges are muted.
- New `ReviewerPreference` default `confirm`; existing rows backfilled `auto`; the importer
  never overwrites `assignment_acceptance` on update.
- Concurrent-writer safety everywhere (`get_or_create` / conflict-aware insert / savepoint
  per `qb_site/AGENTS.md`); sweeps contain per-item `IntegrityError`.
- Every GitHub mutation enqueues `syncer.sync_pr` for convergence.
- `sync_pr` reconciliation: if a human/self-assignee lands on a PR with a `proposed`
  proposal, mark it `superseded` (do not fight the human).
- De-dupe the proposal "please accept" DM against the attention "newly assigned" ping for
  the same event.
- Keep the existing 21-day auto-unassign backstop for *accepted-but-then-stale* PRs; for
  `confirm` reviewers the clock starts at acceptance.

## Implementation Plan (Chunks)

1. ✅ **(landed)** **Config field:** `ReviewerPreference.assignment_acceptance` + data
   migration (existing → `auto`), admin `list_display`/`list_filter`, importer create-only
   handling, bulk admin action to set mode. Unit tests for default behavior.
2. **Model:** `analyzer.AssignmentProposal` + migration (partial-unique index), admin
   (`ReadOnlyAdmin`), backup-policy coverage classifying it as **durable retained history**
   (`scripts/backup_policy.py`).
3. **Builder/engine integration:** candidate exclusion, pending-load contribution, cooldown
   exclusion; pure-engine seams with unit tests (assignability, scarcity, cooldown).
4. **Propose service + task + expiry sweep:** `propose_assignments_for_repo` (per-reviewer
   branch: `auto` → 046 path; `confirm` reachable → proposal; `confirm` unreachable →
   fallback), `analyzer.propose_reviewer_assignments` task, expiry sweep task, settings/
   flags/beat. Dry-run + flags. Unit + task tests.
5. **Notification:** per-reviewer digest DM (reuse `ZulipClient` + a dedupe record or
   `notified_at`), de-duped vs the attention "newly assigned" ping.
6. **Console:** GitHub-OAuth reviewer session, list view, accept/decline handlers
   (accept reuses the 046 mutation path), templates, live re-validation, frontend tests.
7. **Surfacing:** the {unassigned, proposed, assigned} state in the queue snapshot and in
   `pr_info`/`PRQueueInfo` (so the dashboard, API, and the Zulip `pr-info` command render it
   identically, distinct from GitHub assignees); the `AssignmentProposal` admin changelist
   for full (retained) history. No cleanup task in Phase 1.
8. **Docs:** update `qb_site/analyzer/AGENTS.md` (task surface), `qb_site/zulip_bot/AGENTS.md`
   and `qb_site/core/*` notes as needed, and the root pointer; converge this living plan
   toward a final record once shipped.

## Validation Plan

- tests:
  - config default: existing rows backfilled to `auto`; a freshly created preference is
    `confirm`; importer re-run does not flip existing rows.
  - engine/builder: pending proposal excludes the PR + adds load; declined → opt-out
    excludes; expired → cooldown excludes then re-allows after the window.
  - propose task: per-mode branching, `confirm`-unreachable fallback, dedupe/idempotency,
    dry-run, flag-off no-op, per-repo behavior.
  - console: accept assigns + records + enqueues sync; decline opt-outs; stale/merged/
    reassigned proposal renders "no longer available"; session auth required.
  - state transitions: with `ON_QUEUE_EXIT=invalidate`, a pending proposal is invalidated
    (superseded/expired) when the PR leaves the queue or is closed; with `retain`, it
    persists and is acceptable off-queue — both routed through the `proposal_validity`
    predicate; a human/self-assignee lands → `superseded`; a PR is never simultaneously
    proposed and assigned.
  - surfacing: the {unassigned, proposed, assigned} distinction appears in the snapshot
    payload and in `PRQueueInfo`/`pr-info`, rendered distinctly from GitHub assignees.
  - history: terminal proposal rows persist (no cleanup); the cooldown query still returns
    only recent `expired` rows even with old history present.
- manual:
  - run `propose_reviewer_assignments --dry-run` against mathlib4 and inspect the would-
    propose set vs a recent `automatic_assignments` snapshot.
  - staging: end-to-end — receive a digest DM, log into the console via GitHub, accept,
    confirm the assignee lands on GitHub and `ReviewerAssignmentApplication` records it,
    and a second propose run is a no-op.
- Full validation via `bash scripts/repo_check_compose.sh` (includes backup-policy
  validation for the new model).

## Alternatives (discarded / deferred)

- **Post-assignment fast-confirm** (assign now, revoke fast if not accepted): cheaper, but
  only shortens the ghost assignment rather than eliminating it; rejected in favor of the
  true pre-assignment gate.
- **Per-PR signed accept links** instead of a console: don't scale to multiple pending
  proposals and suffer stale-link rot; superseded by the GitHub-login console.
- **Token-bootstrapped session** for console auth: rejected — an automated notification
  carrying a fast-expiring link is annoying; GitHub login gives a stable, bookmarkable URL.
- **Hybrid (propose, then assign anyway if all candidates time out):** deferred; revisit
  only if PRs sitting unproposed becomes a real problem in practice.
- **Compute "new vs old" acceptance default from timestamps** rather than a stored column:
  rejected as fragile; the value is materialized per row at creation.

## Related Decisions

- `020-reviewer-opt-outs-and-timeline-assignments.md`
- `026-zulip-assign-unassign-and-github-app-tokens.md`
- `027-github-app-operation-token-services.md`
- `028-reviewer-queue-nudges-v1-daily-report.md`
- `037-reviewer-assignment-policy-simulation-and-priority-planning.md`
- `039-queue-ruleset-default-designation-and-snapshot-cache-keys.md`
- `046-apply-reviewer-assignments-in-django.md`

## Progress Notes

- 2026-07-08: **Chunk 1 landed.** Added `ReviewerPreference.assignment_acceptance`
  (`CharField(max_length=16, choices auto/confirm, default confirm)`) with `ACCEPTANCE_*`
  constants; migration `core/0007` adds the field (so future rows → `confirm`) plus a
  `RunPython` backfill flipping all existing rows to `auto` (reverse = noop). Admin gains the
  column in `list_display`/`list_filter` and two bulk actions
  (`set_acceptance_confirm`/`set_acceptance_auto`) as the pool-flip lever; a standalone
  management command was deemed unnecessary. The importer create-only invariant holds **by
  construction** (it never references the field and does not use `update_or_create`); a
  regression test pins it. No backup-policy change (column add on the already-covered
  `core_reviewerpreference` table). Validation: 5 new tests + full `core` suite (55) green on
  dockerized Postgres, `makemigrations --check` clean, ruff clean.
- 2026-07-08: Initial plan drafted from design discussion. Decisions confirmed:
  pre-assignment gate; per-reviewer `assignment_acceptance` (`auto`/`confirm`) orthogonal to
  eligibility and notifications, with a full behavior matrix and `confirm`-unreachable
  fallback to `auto`; GitHub-login reviewer console as the primary surface (stable URL in
  the DM); decline → permanent per-PR opt-out, expire → soft cooldown; pending proposals
  count toward load (weight 1.0) and exclude the PR from re-proposal; board must show the
  "awaiting acceptance" state (no GitHub PR-page writes); rollout default = new accounts
  `confirm`, existing accounts backfilled `auto`, with a bulk flip lever retained. Scope =
  acceptance gate only (Phase 1); re-confirm-on-reentry, email channel, and throughput cap
  deferred.
- 2026-07-08: Dropped proposal-history expiry. Volume is low/append-only (~tens of rows per
  PR lifetime) and the history has standalone analytical value, so `AssignmentProposal` rows
  are **retained indefinitely** — no cleanup task or retention setting in Phase 1; the model
  is classified as durable retained history in the backup policy. Cooldown correctness is
  unaffected (it is a bounded query window, not a deletion policy). A cleanup task can be
  added later if volume ever surprises us.
- 2026-07-08: Resolved the on-queue-exit transition as **invalidate** (default) but kept it
  pluggable: a single centralized `proposal_validity` predicate (consulted by the reconcile
  sweep, sync-time reconciliation, and console accept-time re-validation) reads a new
  `ANALYZER_ASSIGNMENT_PROPOSAL_ON_QUEUE_EXIT` setting (`invalidate`/`retain`). Invariant #2
  is now stated as policy-dependent so a future flip to `retain` won't contradict it;
  load-accounting/surfacing read active proposals independent of queue state.
- 2026-07-08: Added the explicit PR assignment state model ({unassigned, proposed, assigned}
  as one intermediate value on the existing assignment axis, with five legibility invariants
  and the "proposed is nested in awaiting-review" constraint) to address a
  future-comprehensibility concern. Decided to surface proposal state in `pr-info`/
  `PRQueueInfo` (same source as the board, distinct from GitHub assignees) and to keep
  proposal history with 90-day retention via a cleanup task — current state in default views,
  full history in admin. Open transition question recorded: invalidate a pending proposal
  when the PR leaves the review queue (recommended) vs. let it ride.
- 2026-07-08: Refined the notification model. Confirmation prompts are recategorized as
  transactional and decoupled from `notifications_enabled` (which now governs only the
  optional attention nudges), so a reviewer can opt out of nudges while staying in `confirm`
  and still receiving proposal prompts — without a new per-reviewer setting. Fallback to
  `auto` now triggers only when a reviewer is genuinely unreachable (no Zulip link). The
  console-only `confirm` toggle (`confirmation_prompts_enabled`) is recorded as a deferred
  YAGNI option.
