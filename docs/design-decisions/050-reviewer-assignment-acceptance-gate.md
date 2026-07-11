# Reviewer Assignment Acceptance Gate (Propose → Accept → Assign)

> Status: **Implemented** (including a post-implementation review-hardening pass). All feature
> flags default off; staged production rollout and the staging end-to-end check are pending — see
> Operational Notes.

## Context

- Reviewers were auto-assigned to PRs directly, and assignments were frequently ignored or
  forgotten. A community request (private Zulip thread: Filippo Nuccio, Riccardo Brasca, with
  input from Dagur Asgeirsson, Rémy Degenne, Yaël Dillies, Jon Eugster, Michael Rothgang,
  Christian Merten) asked that an assignment be **proposed** to a reviewer, who must **accept**
  it within a few days before it is executed — so "an assignment really means the assignee knows
  they're in charge."
- The pre-existing pipeline was three decoupled daily Celery stages in `analyzer/`:
  compute (`analyzer.refresh_reviewer_assignments` → `ReviewerAssignmentSnapshot` with
  `{pr_number: login}`), apply (`analyzer.apply_reviewer_assignments`, design doc 046), and
  attention (`analyzer.reviewer_attention_daily`, design doc 028).
- Key realization: with a **daily recompute loop already in place**, the gate needs no bespoke
  per-PR sequencing. "One at a time, advance to the next reviewer on timeout" falls out of
  *recompute + exclude*: an expired/declined reviewer is excluded next cycle and the engine picks
  the next-best candidate. No hand-written state machine.
- Reused infrastructure: proactive Zulip DMs (`ZulipClient.send_direct_message`), GitHub OAuth
  (`core.services.github_oauth.GitHubOAuthClient`), per-PR opt-outs (`analyzer.ReviewerOptOut`),
  GitHub App operation tokens (`assign_pr`), and the 046 mutation/audit path
  (`ReviewerAssignmentApplication`).

## Decision

### Pipeline

`analyzer.propose_reviewer_assignments` (daily, default 00:45 UTC) replaces the direct-POST batch.
Per `{pr, login}` from the authoritative default-rule-set snapshot, it branches on the reviewer's
`ReviewerPreference.assignment_acceptance`:

- `auto` → direct-assign via the verbatim 046 mutation path (`assign_reviewer_and_record`).
- `confirm` + Zulip-linked → create `AssignmentProposal(state=proposed, expires_at=now+window)`.
- `confirm` but unreachable (no `core.User.zulip_user_id`) → fall back to direct-assign.

Downstream: `analyzer.deliver_assignment_proposals` (daily, default 01:00 UTC) sends one digest DM
per reviewer across all repos, linking to the console; the reviewer **accepts** (re-validate live →
GitHub assign → `accepted`) or **declines** (`declined` + active `ReviewerOptOut`) on the console;
`analyzer.expire_assignment_proposals` (hourly) retires proposals that timed out or became invalid.
Next day's recompute excludes retired reviewers and proposes to the next candidate.

The legacy `analyzer.apply_reviewer_assignments` is retained for `auto`-only operation but is
**superseded** by propose. Mutual exclusion is enforced in code, not just documented: when
`ANALYZER_ASSIGNMENT_PROPOSALS_ENABLED` is set, the apply task skips itself (reason
`superseded_by_proposals_pipeline`, error log), so the proposal-unaware path can never bypass the
gate; propose logs a warning about the misconfiguration.

### PR assignment state model

One intermediate value on the existing assignment axis — for an open, on-queue PR:
**unassigned** / **proposed** (no GitHub assignee, one active proposal) / **assigned**. Invariants:

1. Exactly one of {unassigned, proposed, assigned} at any moment — enforced by the partial-unique
   index (one active proposal per PR).
2. Under the default `invalidate` on-queue-exit policy, "proposed" is a strict substate of
   on-queue/awaiting-review. The `retain` policy deliberately relaxes this, so load-accounting and
   surfacing never *assume* proposed ⇒ on-queue.
3. Live state is always reconstructable from durable facts: GitHub assignees + the single active
   proposal. No derived state that can drift.
4. A **proposal is never an assignee**; every surface renders "proposed to X" distinct from
   "assigned to X".
5. Terminal proposals (`accepted`/`declined`/`expired`/`superseded`) are history, not live state;
   they feed only the bounded expire-cooldown lookup.

### Model: `analyzer.AssignmentProposal`

- `repository` FK, `pr_number`, `reviewer_login` (snapshot/opt-out login keying), `snapshot` FK
  (`SET_NULL` provenance), `state`, `expires_at`, `decided_at`, `notified_at`,
  `decided_via` (`console` / `auto_expire` / `sync_superseded`); `created_at` is the proposal time.
- Partial unique index `(repository, pr_number) WHERE state='proposed'` enforces one active
  proposal per PR race-safely; creation is conflict-aware per the "Concurrent Writers and Unique
  Keys" rules. Indexes on `(repository, reviewer_login, state, decided_at)` and
  `(repository, pr_number, state)` serve the console, cooldown, and pr-info queries.
- **History is retained indefinitely** — no cleanup task. Volume is low/append-only, the rows have
  analytical value (accept rate, time-to-accept), and the cooldown is a bounded query window, not
  a deletion policy. Classified as durable history in `scripts/backup_policy.py` (BACKUP +
  TRUNCATE-from-sanitized-dump, like sibling reviewer tables).

### Per-reviewer mode and notification semantics

`ReviewerPreference.assignment_acceptance` ∈ {`auto`, `confirm`}; migration backfilled existing
rows to `auto` (grandfathered), new rows default `confirm`. The importer never touches the field
on update (regression-pinned). Bulk admin actions flip a selection, and reviewers can flip their
own mode from the Zulip prefs form (a two-option radio in the Auto-Assignment section) — the
control is shown only while `ANALYZER_ASSIGNMENT_PROPOSALS_ENABLED` is on, so reviewers never see
an option that has no effect.

Confirmation prompts are **transactional** and NOT gated by `notifications_enabled` (which governs
only optional attention nudges — matching doc 028's precedent that essential actions ignore the
notification opt-in). Fallback to `auto` happens only when a reviewer is genuinely unreachable
(no Zulip link), never merely because nudges are muted.

| `assignment_acceptance` | nudges (`notifications_enabled`) | behavior |
| --- | --- | --- |
| `auto` | on | Direct-assign + optional "you were assigned" DM (= pre-gate behavior). |
| `auto` | off | Direct-assign, silently. |
| `confirm` | on | Propose → digest DM → console accept → assign, plus optional nudges. |
| `confirm` | off | Propose → digest DM → accept → assign; no optional nudges. |

`auto_assign=false` → never auto-assigned; the gate is irrelevant.

### Validity: one authority for "is this proposal still live"

`analyzer.services.assignment_proposal_validity` is the sole owner of pending-proposal liveness:

- `proposal_validity(...)` — pure predicate over durable facts (`pr_state`, `current_assignees`,
  `on_queue`, `opted_out`). Precedence: already-terminal → assignee landed (**any** non-empty
  assignee set supersedes — "don't fight the human/self-assignee") → reviewer **opted out**
  (superseded; a self-unassign reconciled into an opt-out retires the pending proposal on the next
  sweep instead of leaving it dangling) → PR closed/merged (superseded) → past `expires_at`
  (expired; seeds the soft cooldown) → off-queue under the `invalidate` policy (superseded) → live.
- The **input assembly is shared too**, not just the verdict: `queue_membership(repo, now)`
  (queue/known-PR sets + freshness from the latest default `QueueSnapshot`) and
  `live_proposal_validity(...)` (facts assembly + predicate) are consumed by both the expiry sweep
  and the console, so the two surfaces cannot drift. `on_queue=None` when no fresh snapshot covers
  the PR — a stale/missing snapshot never mass-invalidates. `queue_membership` extracts only the
  two needed payload fragments in SQL (`payload #> '{lists,dashboards,Queue}'`,
  `jsonb_object_keys(payload->'prs')`; Postgres-only, per repo policy) rather than loading the
  multi-MB payload per sweep run / console POST.
- The on-queue-exit behavior is selectable via `ANALYZER_ASSIGNMENT_PROPOSAL_ON_QUEUE_EXIT`
  (`invalidate` default / `retain`), read inside the predicate; a future flip is a one-setting
  change.
- **Propose-time re-validation matches the predicate**: any non-empty assignee set blocks proposal
  creation (not just assignees who are eligible reviewers). Without this alignment, a PR with a
  non-reviewer assignee would be proposed, DM'd, superseded by the sweep, and re-proposed daily —
  superseded rows feed neither the cooldown nor the active-proposal exclusion.

### Decline vs. expire

- **Decline** = explicit "not this PR" → permanent per-PR `ReviewerOptOut` (builder-enforced).
  The console writes the opt-out with the **lowercase-normalized** login, matching every other
  writer/clearer against the table's case-sensitive unique constraint.
- **Expire** (silent timeout) = soft: the builder skips a reviewer for a PR with an `expired`
  proposal within `ANALYZER_ASSIGNMENT_PROPOSAL_EXPIRE_COOLDOWN_DAYS` (default 14), then re-allows.
  The PR advances to other candidates immediately; a reviewer who was merely away gets another
  shot later.

### Builder / engine / stats integration

All routed through the shared `_prepare_assignment_inputs` so the builder and the diagnostic trace
cannot diverge; data-driven and ungated (no proposals ⇒ no-op):

1. Candidate exclusion — PRs with an active proposal are withheld from re-proposal.
2. Pending-load contribution — active proposals add weighted load
   (`ANALYZER_ASSIGNMENT_PROPOSAL_PENDING_LOAD_WEIGHT`, default 1.0; a proposal occupies a slot)
   via the pure engine seam `add_pending_proposal_load`. **`AreaStatsBuilder` applies the same
   pending load**, so area-level `at_max_capacity` agrees with what the next assignment run will do.
3. Cooldown exclusion — recent `expired` proposals merge into the per-PR exclusion set alongside
   opt-outs.

### Delivery and attention integration

- Digest dedupe key = `AssignmentProposal.notified_at`, stamped by a race-safe conditional
  `UPDATE ... WHERE state='proposed' AND notified_at IS NULL` after a successful send. One DM per
  reviewer across all repos; no separate record model.
- Message chunking uses the single shared `zulip_bot.services.zulip_client.split_message_chunks`
  (+ `MAX_MESSAGE_CHARS`), which hard-splits any oversized line so no chunk can exceed Zulip's
  ceiling and fail a send. (Previously three drifting copies; do not re-implement per call site.)
- The attention sweep suppresses its "newly assigned" ping only for the assignment **the
  acceptance actually produced**: the console-accept `decided_at` must match the ASSIGNED timeline
  event's `occurred_at` within a 1-hour tolerance (the accept performs the GitHub assign moments
  before `decided_at` is stamped). A later unrelated re-assignment of the same pair still pings.

### Reviewer console (`qb_site/console/`)

- Dedicated Django app at `/console/`; plain server-rendered views; no models of its own. The DM
  carries a plain, stable, bookmarkable URL (no token), built from `QUEUEBOARD_BASE_URL` via
  `core.services.site_urls.build_site_url`. The same URL
  is available on demand via the `console` Zulip command (an in-place reply, not a private DM: the
  link is non-secret and identical for every reviewer since the page self-authenticates); like all
  commands it is reachable only where `ZULIP_COMMAND_POLICY` permits it.
- **Auth:** GitHub OAuth → Django session holding the resolved `core.User` id. CSRF nonce in the
  session echoed through the Fernet-signed OAuth `state` (`core.services.oauth_state`, shared with
  the registration flow, which delegates to it). Hardened:
  - `core.services.github_identity.resolve_user_from_identity` is **resolve-only by
    construction** — it never creates users, so the public sign-in URL cannot mint a `core.User`
    for an arbitrary GitHub account; and a login match whose stored `github_node_id` differs is
    treated as no match (a **recycled username** must not inherit the previous owner's session and
    proposals).
  - The session key is **rotated on login promotion** (`cycle_key`, like
    `django.contrib.auth.login`) against session fixation.
- Access is keyed on the authenticated `github_login`, matched case-insensitively against
  `AssignmentProposal.reviewer_login`; a reviewer can only act on their own proposals, and console
  access is independent of Zulip reachability.
- **Accept** (gated by `ANALYZER_ASSIGNMENT_PROPOSALS_ASSIGN_ON_ACCEPT_ENABLED`): re-validate via
  the shared validity path → 046 mutation (`assign_reviewer_and_record`) → mark `accepted` only
  when the assignment **landed** on GitHub (`applied`, or `already_recorded` whose record is
  APPLIED — a prior FAILED attempt is never reported as success; the proposal stays pending for a
  later retry). **Decline:** mark `declined` + upsert the (normalized) opt-out. Every POST
  re-validates; a no-longer-actionable proposal renders "no longer available, here's why" (and the
  stale row is retired) instead of erroring.
- All proposal state transitions are idempotent conditional updates
  (`UPDATE ... WHERE state='proposed'`), safe against concurrent sweeps/clicks.

### Surfacing (never the GitHub PR page)

- **Board/API:** the queue snapshot embeds a `proposal` field (`{reviewer, expires_at}` or `null`)
  per PR entry (one batched query; payload served verbatim).
- **Single PR:** `PRQueueInfo.proposed_to`/`proposal_expires_at`, read live (indexed point query);
  the Zulip `pr-info` command renders a distinct "Proposed to X (awaiting acceptance, expires …)"
  line, never merged into Assignees.
- Full history lives in the `AssignmentProposal` `ReadOnlyAdmin`; default views show current state
  only. Nothing is ever written to the GitHub PR page (no comments, no labels).

## Consequences

- A `confirm` PR carries no GitHub assignee during the pending window (up to the acceptance
  window, default 7 days); the board/pr-info "proposed" state is the only visibility, which is why
  surfacing shipped in the same phase. Placement latency for unresponsive reviewers is bounded by
  window + cooldown recompute rather than instant.
- Two assignment pipelines coexist (legacy apply, gate propose). The code-level yield rule removes
  the bypass risk, at the cost of the apply task being a silent no-op in a both-enabled
  misconfiguration (it logs an error explaining why).
- The single validity authority (verdict *and* input assembly, opt-out-aware) is load-bearing: new
  invalidation rules belong in `assignment_proposal_validity`, never inline at a call site.
- `queue_membership` uses Postgres jsonb operators — consistent with the repo's Postgres-only
  policy, but not portable to other backends.
- Proposal history grows without bound by design; acceptable at current volume, revisit only if it
  surprises (a cleanup task can be added without redesign).
- The console is a new externally-reachable authenticated surface; its safety rests on
  resolve-only identity, the recycled-login guard, session-key rotation, per-reviewer proposal
  scoping, and re-validation on every POST.

## Operational Notes

- **Settings** (all in `settings/base.py` + `.env.example`): flags
  `ANALYZER_ASSIGNMENT_PROPOSALS_ENABLED` / `_DELIVERY_ENABLED` / `_ASSIGN_ON_ACCEPT_ENABLED` /
  `_DRY_RUN`; tuning `ANALYZER_ASSIGNMENT_PROPOSAL_WINDOW_DAYS` (default 7 — the **global value is
  honored as-is**; only the per-reviewer `notification_settings` override is clamped ≥7),
  `_EXPIRE_COOLDOWN_DAYS` (14), `_PENDING_LOAD_WEIGHT` (1.0), `_ON_QUEUE_EXIT`
  (`invalidate`/`retain`); schedules `ANALYZER_ASSIGNMENT_PROPOSE_*` (daily 00:45 UTC),
  `ANALYZER_ASSIGNMENT_DELIVER_*` (daily 01:00 UTC),
  `ANALYZER_ASSIGNMENT_PROPOSAL_EXPIRY_PERIOD_SECONDS` (3600). The expiry sweep is essential
  maintenance: not gated by the master switch (flipping the gate off lets proposals drain) and
  performs no GitHub writes.
- **Deploy prerequisites:** set `QUEUEBOARD_BASE_URL` (console links + OAuth `redirect_uri`);
  register the GitHub OAuth App callback at the **site root** so both the registration and
  console callback paths are covered (`docs/zulip_github_oauth_setup.md`).
- **Rollout** (staged, per the 028 discipline; pending): flip selected reviewers to `confirm` via
  the bulk admin action → `_DRY_RUN=1` and inspect propose/deliver output → enable `_ENABLED` →
  `_DELIVERY_ENABLED` → `_ASSIGN_ON_ACCEPT_ENABLED`; disable the legacy
  `ANALYZER_REVIEWER_ASSIGNMENT_APPLY_ENABLED`. Staging end-to-end (digest DM → console sign-in →
  accept → assignee lands + `ReviewerAssignmentApplication` recorded → second propose run no-op)
  remains a manual step — tests mock GitHub/Zulip.
- **Management commands:** `propose_reviewer_assignments` / `deliver_assignment_proposals`
  (`--repo o/n`, `--dry-run`, `--enable`); `backfill_reviewer_opt_outs`.
- Keep the 21-day auto-unassign backstop for accepted-but-then-stale PRs; for `confirm` reviewers
  the clock starts at acceptance.

## Deferred Follow-Ups

- Re-confirm ~10 days after `awaiting-author` is removed (maps onto the attention sweep's
  queue-re-entry reset).
- Throughput cap ("reviewed X PRs in 30 days → pause auto-assign") — a separate capacity input.
- Email channel (Zulip-first today); unifying the proposal digest with the attention DM;
  console-only `confirm` (`confirmation_prompts_enabled`) — YAGNI until a reviewer asks.
- "(N prior proposals)" hint on default views (kept out of the hot snapshot build); a unified
  "assignment activity" view joining proposals with the assignment timeline.
- Hybrid "propose, then assign anyway if all candidates time out" — revisit only if PRs sitting
  unproposed becomes a real problem.
- **Stable `/prefs` URL sharing the console session.** Today the Zulip prefs form authenticates via
  an expiring Fernet token embedding a `preference_ids` snapshot; the console authenticates via
  GitHub OAuth → Django session (`core.User` id). A follow-up could give prefs a token-less, stable
  URL that reads the same session and loads the user's live preferences. Feasible and clean (the
  console already resolves identity by `github_login`, so the eligible population is essentially the
  same), but it is an auth-model change, not a URL tweak — it makes GitHub OAuth the entry point for
  a flow that today needs none, changes scoping from a token snapshot to all-current prefs, and
  wants its own CSRF/session plumbing, tests, and security-surface review. Deliberately kept off this
  branch so the gate's rollout stays focused.

## Alternatives (discarded)

- **Post-assignment fast-confirm** (assign now, revoke if not accepted): only shortens the ghost
  assignment; rejected for the true pre-assignment gate.
- **Per-PR signed accept links**: don't scale to multiple pending proposals; stale-link rot.
  Superseded by the GitHub-login console with a stable URL.
- **Token-bootstrapped console session**: an automated DM carrying a fast-expiring link is
  annoying; GitHub login gives a bookmarkable URL.
- **Deriving the acceptance default from account age** instead of a stored column: fragile; the
  value is materialized per row at creation.

## Related Decisions

- `020-reviewer-opt-outs-and-timeline-assignments.md`
- `026-zulip-assign-unassign-and-github-app-tokens.md`
- `027-github-app-operation-token-services.md`
- `028-reviewer-queue-nudges-v1-daily-report.md`
- `037-reviewer-assignment-policy-simulation-and-priority-planning.md`
- `039-queue-ruleset-default-designation-and-snapshot-cache-keys.md`
- `046-apply-reviewer-assignments-in-django.md`
