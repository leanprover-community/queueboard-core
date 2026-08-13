# Timeline Actor Type for Bot Classification

> Status: **Proposed** — not started. Living implementation plan; drafted from a
> downstream (`qb-notebook`) data-quality investigation, 2026-08-12. Reviewed
> and revised against the tree 2026-08-13 (see Progress Notes for what changed
> and why).

## Context

- `PRTimelineEvent.actor_login` stores *who* acted, but not *what kind of
  account* acted, and not a rename-stable identity for that account. Every
  downstream consumer that needs "was this a human?" therefore hardcodes a
  list of bot logins.
- **GitHub already tells us, and we already ask for it, and we throw it away.**
  All three live timeline queries select the actor's `__typename`:
  - `qb_site/syncer/queries/pr_bundle.graphql:131-231`
  - `qb_site/syncer/queries/timeline_page.graphql`
  - `qb_site/syncer/queries/timeline_page_back.graphql`

  each shaped `actor { __typename ... on User { login } ... on Bot { login } ... on Mannequin { login } }`.
  `_login_or_empty()` (`qb_site/syncer/services/sub/timeline_sync.py:47-58`)
  then keeps only `login` and discards `__typename`.
- Concrete downstream failure that motivated this. `qb-notebook`'s
  `DEFAULT_BOT_ACTORS` (`qb_notebook/review_states.py`) is used to find each
  PR's first *human* review touch. The mathlib bots were renamed on
  **2026-02-03** and the list silently went stale:

  | filtered (old login) | last event | unfiltered (new login) | first event |
  | --- | --- | --- | --- |
  | `mathlib4-dependent-issues-bot` | 2026-02-02 | `mathlib-dependent-issues` | 2026-02-03 |
  | `mathlib4-merge-conflict-bot` | 2026-02-02 | `mathlib-merge-conflicts` | 2026-02-03 |
  | `leanprover-community-mathlib4-bot` | 2026-02-02 | *(no successor)* | — |

  Effect on the "open → first review" metric for a last-90-days cohort
  (snapshot 2026-08-10): **15.7 % of first-touches were bot events**, and the
  median moved 0.261 d → 0.467 d once filtered — i.e. a reported ~41 %
  latency improvement was mostly an artifact. Six months of a wrong number
  from a rename nobody downstream could have known about.
- Other unlisted bots found in the same sweep, all with zero API support for
  identifying them as bots: `mathlib-auto-merge`, `mathlib-splicebot`,
  `leanprover-bot`, `mergify`, `downstream-reports-automation`,
  `botbaki-review`, `copilot-pull-request-reviewer`, `copilot-swe-agent`.
- **Login is the wrong key, and that is the root cause.** The rename broke a
  login-keyed list. Typing the actor fixes it only for GitHub Apps; the
  machine-user half of the list stays login-keyed and stays rename-fragile
  (see Subtleties). Storing the actor's node id alongside the type is what
  actually closes the motivating bug, which is why it is in scope here.
- No usable substitute exists in the current export:
  - `core_user.github_node_id` (`qb_site/core/models/user.py:29`) does encode
    the account kind in its prefix (`BOT_…` / legacy `04:Bot…`), but
    `core_user` is a PR-**author** table. 60 event actors accounting for
    **111,864 touch events (~41 %)** have no `core_user` row at all, because
    most bots never open PRs. (Note: the `node_kind` referenced in the
    original draft is a `qb-notebook`-side decode of that prefix, not a column
    in this repo.)
  - The GitHub App `[bot]` login suffix is not preserved: **0** occurrences in
    `actor_login`, `assignee_login`, `requested_reviewer_login`, or
    `core_user.github_login`. This is not something we strip — GraphQL's
    `Bot.login` never includes the suffix (REST's `/users/<slug>[bot]` does).
  - `PRTimelineEvent.extra` was reported as `{}` for all 598,030 exported
    rows. That is almost certainly an **export artifact, not a fact about the
    table**: `timeline_sync.py:149-154` populates `extra` on every
    `REVIEW_DISMISSED` row. The export path is
    Postgres → CSV → `pd.read_csv` → parquet
    (`scripts/export_for_analysis.py:45-56`), so `extra` arrives as a JSON
    *string* column, not a dict. Worth re-checking downstream; either way
    `extra` is not a substitute for a typed column.
- Precedent inside the very same function: the
  `ReviewRequestedEvent` / `ReviewRequestRemovedEvent` branch already reads
  `requestedReviewer.__typename` to route `User`/`Bot`/`Mannequin` vs `Team`
  (`timeline_sync.py:158-162`). This change generalizes that idiom to actors.
- Precedent for selecting the actor's `id`: the top-level `author` selections
  in `pr_bundle.graphql:63-98` already request `id` for both `User` and `Bot`.
  The timeline actor unions just never did.

## Goals / Non-Goals

Goals:
- Persist the actor's GraphQL `__typename` on `PRTimelineEvent` so bot
  classification is exact.
- Persist the actor's GraphQL node `id` so downstream lists can be keyed on a
  **rename-stable identity** instead of a login. This is what makes the fix
  durable rather than a one-time patch.
- Make both available in the `analytics-datasets` export for `qb-notebook`.
- Backfill historical rows — the analytical value is almost entirely
  historical, so new-rows-only is close to worthless here.
- While the backfill is resolving those rows anyway, fill the **missing
  `actor_login`** on archive-imported rows. The legacy archive fragment
  omitted the actor entirely, so these rows have no attribution at all; the
  same `nodes(ids:)` response that carries the typename carries the login.

Non-Goals:
- **Not** a complete "is this a human" oracle. See the machine-user caveat in
  Subtleties — downstream lists shrink and stop being rename-fragile, but do
  not disappear.
- Not changing `actor_login` semantics for rows that already have a value, the
  `""`-when-null convention, or any existing consumer. Filling
  previously-empty `actor_login` is additive and uses the same fill-only
  guard.
- **Not classifying `PRReviewInlineComment.author_login`.** Deliberate, and
  the reasoning is worth recording because the surface looks identical:
  - Everything needed is already there. The wire carries
    `author { __typename ... }` (`pr_bundle.graphql:199`),
    `PRReviewInlineComment.github_node_id` is `unique=True`, so the same
    `nodes(ids:)` backfill mechanism works verbatim, and the table is already
    exported (`scripts/backup_policy.py:136`). The cost would be an
    `author_type` / `author_node_id` pair mirroring this change, plus a second
    table argument on the backfill command.
  - It nonetheless carries **no new information**, because an inline comment's
    author is the parent review's author. Sampled 30 recent merged mathlib4
    PRs on 2026-08-13: 33 inline comments, **0 author mismatches** against
    their parent review's author (3 of the 33 were bot-authored, so bots do
    post inline comments — they just do it as their own review). GitHub
    attributes a review's comments to the reviewer; a reply by someone else
    becomes its own review. So `author_type` here is derivable by joining
    `review_node_id` → `PRTimelineEvent.github_node_id` and reading that
    row's `actor_type`. Treat the sample as absence-of-counterexample, not
    proof.
  - Residual gap if we skip it: inline comments whose parent
    `PRTimelineEvent` row does not exist — the documented null
    `parent_review_event` case where a dismiss event had `review: null` on
    GitHub — have nothing to join to, so their author stays untyped. Expected
    to be a small population.
  - What would justify doing it later: per-comment review-effort or
    review-depth metrics that count inline comments per author without
    joining to the parent, where bot-authored comments
    (`copilot-pull-request-reviewer`, `copilot-swe-agent`) would inflate
    human review volume. Denormalization for convenience, not for
    correctness — so it should be driven by a real downstream query, not by
    symmetry with this change.

## Proposed Design

- **Model.** Add to `qb_site/syncer/models/pr_timeline_event.py`:

  ```python
  class PRActorType(models.TextChoices):
      USER = "User", "user"
      BOT = "Bot", "bot"
      MANNEQUIN = "Mannequin", "mannequin"

  # GraphQL __typename of the acting account, as returned by the timeline
  # queries. Null for rows ingested before this column existed, for
  # archive-imported rows whose legacy fragment omits the actor entirely,
  # and for events where GitHub itself returns a null actor.
  actor_type = models.CharField(max_length=16, choices=PRActorType.choices, null=True, blank=True)
  # GraphQL node id of the acting account. Stable across login renames, so
  # downstream automation lists should key on this rather than actor_login.
  # Null under the same conditions as actor_type.
  actor_node_id = models.CharField(max_length=255, null=True, blank=True)
  ```

  Store GitHub's exact casing (`"Bot"`) rather than a normalized form — it is
  the wire value, and `requested_*` routing already compares raw typenames.
  Null (not `""`) means "unknown".

  **No `db_index=True` on either column.** `actor_type` is three-valued, so a
  btree on it is not selective enough for Postgres to use on the obvious
  `= 'Bot'` predicate; the downstream "first human touch per PR" shape is
  already served by `syncer_prtimeline_pr_time_idx`, and the analytics
  consumer reads a `SELECT *` parquet where indexes are irrelevant. Add an
  index only when an in-repo query needs one. (Contrast
  `requested_reviewer_login`, which is indexed because reviewer assignment
  queries it.)

  **No new CHECK constraint.** Unlike `before_sha` / `label_name` /
  `requested_*`, these columns are meaningful on every event type, so there is
  no type-scoping to enforce. Do not add an
  "`actor_type` set ⟹ `actor_login` non-empty" constraint either: it holds
  today but is a GitHub-side invariant we do not control.

  `actor_node_id` is separable — it is the second half of Chunk 1 and can be
  dropped if the added width (~40 B × 600 k rows, plus export size) is
  unwelcome. Dropping it means keeping the rename fragility downstream.

- **Wire change.** Add `id` to the actor/author unions in all three live
  queries, mirroring `pr_bundle.graphql:63-98`:
  `actor { __typename ... on User { id login } ... on Bot { id login } ... on Mannequin { id login } }`.
  Adding fields to an existing selection does not change GraphQL point cost
  (cost is driven by connection `first`/`last`), so this is free on the rate
  budget.

- **Extraction.** Add siblings to `_login_or_empty`:

  ```python
  def _actor_type_or_none(actor: Any) -> Optional[str]:
      """Return actor.__typename when it is a known account kind, else None."""
      if not isinstance(actor, dict):
          return None
      tn = actor.get("__typename")
      return tn if tn in PRActorType.values else None


  def _actor_node_id_or_none(actor: Any) -> Optional[str]:
      """Return the actor's GraphQL node id, or None when absent."""
      if not isinstance(actor, dict):
          return None
      nid = actor.get("id")
      return str(nid) if nid else None
  ```

  Deriving the allowed set from `PRActorType.values` rather than a literal
  tuple keeps the helper from drifting if the choices change.

  Set `fields["actor_type"]` / `fields["actor_node_id"]` at every branch of
  `_extract_event_fields` that currently sets `actor_login`. **There are two
  idioms in that function** — `_login_or_empty(ev.get("actor"))` and the raw
  `(ev.get("actor") or {}).get("login")` — plus `ev.get("author")` for
  `IssueComment` / `PullRequestReview`. All of them need the parallel lines;
  see the Chunk 2 checklist.

- **Backfill: targeted `nodes(ids:)` resolution, not a rewalk.** Every
  `PRTimelineEvent` already stores the timeline item's own
  `github_node_id`, and GitHub will re-resolve that node's actor on demand.
  Verified against the live API on 2026-08-13:

  - `nodes(ids: [...])` resolves timeline items and returns the actor union
    exactly as the timeline path does — probed side by side on three
    `LabeledEvent`s of `mathlib4#30723`, identical results including the
    two null actors.
  - Heterogeneous batches work: one call mixing `LabeledEvent`,
    `IssueComment`, and `PullRequestReview` ids returned each one's
    `actor`/`author` typename and login (`mathlib-bors` → `Bot`,
    `github-actions` → `Bot`, humans → `User`).
  - Measured `rateLimit.cost` was **1** for a 10-id call. `nodes` accepts at
    most 100 ids, so ~598 k rows ≈ **~6 k points**, against a 5 000 pt/hour
    primary budget — roughly an hour or two of drip-fed budget, versus the
    <24 h a full v4 rewalk wave took for v3. Re-measure cost at 100 ids
    before assuming the ratio holds.

  This route is *exact*, not heuristic: it resolves the actual actor object
  attached to each specific event, so renamed accounts resolve correctly and
  login reuse cannot mis-type anything. It fills `actor_node_id` in the same
  call. It needs no schema-version wave, no reset migration, and does not
  interact with the upgrader machinery.

  It also covers the synthesized dismissed-review rows for free: their
  `github_node_id` is the `PullRequestReview` node id
  (`timeline_sync.py:228-241`), which resolves via the same call and carries
  `author`.

  **And it fills the archive rows' missing `actor_login` in the same pass**,
  since the resolved actor carries `login` alongside `__typename` and `id`.
  Guarded as fill-only, mirroring `timeline_sync.py:346`: write only when the
  resolved login is non-empty **and** the stored value is empty. The predicate
  must cover both empties — `actor_login IS NULL OR actor_login = ''` — because
  the two extraction idioms disagree (see Subtleties). Never overwrite a
  non-empty stored login: it is the login as of ingest time, and clobbering it
  with today's login would destroy the rename history that `actor_node_id`
  exists to expose.

  Rows with `github_node_id IS NULL` cannot be backfilled this way. In
  practice there should be none from this path —
  `_extract_event_fields` returns `None` when the node id is missing — but the
  command should count and report them rather than assume zero.

  See Alternatives Considered for the two routes this replaces.

- **Export.** No `scripts/backup_policy.py` change required:
  `EXPORT_TABLE_QUERIES["syncer_prtimelineevent"]` is
  `SELECT * FROM syncer_prtimelineevent ORDER BY id`
  (`scripts/backup_policy.py:135`), so new columns flow through
  automatically. **But verify dtype, not just presence** — see the parquet
  drift trap in Subtleties.

- **Sanitization.** No change. `scripts/sanitize_backup.py` does not touch
  `syncer_prtimelineevent` or `actor_login`; `actor_type` is a three-valued
  enum and `actor_node_id` is an opaque GitHub identifier already exported
  for authors via `core_user.github_node_id`.

## Subtleties / Invariants

- **`__typename == "Bot"` identifies GitHub *Apps*, not all automation.**
  Machine accounts that are ordinary GitHub user accounts report `User`. Two
  current entries in the downstream list are exactly this case —
  `leanprover-community-mathlib4-bot` and `leanprover-community-bot-assistant`
  both decode to `User` from their `core_user.github_node_id` prefix. So
  `actor_type` alone is *necessary but not sufficient*, and on its own it
  **does not close the bug that motivated this doc**: the residual
  machine-user list would stay login-keyed and break at the next rename in
  exactly the same way.

  `actor_node_id` is what closes it. Downstream should key its machine-user
  list on node id; a rename then changes nothing. Say this explicitly in the
  downstream docs, along with the fact that the list shrinks but does not
  disappear. Bonus: the set of `(actor_login, actor_node_id)` pairs in the
  table is a free rename history.
- **GitHub returns a null actor for a real share of events, permanently.**
  Sampled two mathlib4 PRs' last-100 timelines on 2026-08-13: of 5
  `LabeledEvent`s, **3 had `actor: null`** (`delegated`, `ready-to-merge` —
  workflow labels), confirmed identical via both the timeline path and
  `nodes(ids:)`. No backfill route can type these; `actor_type IS NULL` will
  remain a non-trivial population forever. This is evidence, not a
  hypothetical, for the invariant below.
- **Analysts must treat `actor_type IS NULL` as unknown, never as `User`.**
  Document this wherever the export schema is described. Note that repo has
  no export README — that documentation lives in `qb-notebook` /
  `analytics-datasets`, so this is a cross-repo action item.
- **Parquet dtype drift.** `scripts/export_for_analysis.py:45-56` exports via
  `COPY … CSV` → `pd.read_csv` → `to_parquet`. If the first export runs while
  `actor_type` is entirely NULL, pandas infers an all-NaN **float64** column
  and the parquet lands as `double`; a later export with values lands as
  `object`. Same for `actor_node_id`. So sequence the first export *after* the
  backfill has written some rows, and assert dtype in Chunk 4 — not just that
  the column exists.
- **The fill-empty column allowlist must be extended.**
  `timeline_sync.py:336-344` updates existing rows by iterating an explicit
  tuple of column names (`label_name`, `assignee_login`, `actor_login`,
  `before_sha`, `after_sha`, `requested_reviewer_login`,
  `requested_team_slug`). **`actor_type` and `actor_node_id` must be added
  there.** The chosen backfill route writes via bulk UPDATE and does not
  depend on this, but every ordinary rewalk does — including the continuous
  timeline backfill for PRs that are not yet `timeline_backfill_done`, and any
  future schema wave. Omit it and those paths silently never populate the new
  columns. This is still the single easiest thing to miss in this change.
- The `new_val and not getattr(obj, col)` guard makes the update path
  fill-only / append-only: a rewalk never overwrites an existing
  `actor_type`. Correct default, and it means the backfill is idempotent and
  order-independent with respect to the live syncer.
- **The synthesized dismissed-review rows need explicit handling.**
  `_synthesize_dismissed_review_parent` (`timeline_sync.py:228-241`) creates
  `REVIEW_APPROVED` / `REVIEW_CHANGES_REQUESTED` rows with `actor_login` taken
  from `extra["dismissed_review_author"]`, where no type information exists —
  so these rows get `actor_type = NULL` even under fresh live ingestion.
  These are review events, i.e. exactly what the motivating downstream metric
  reads. Two fixes, both worth doing:
  - Live: `pr_bundle.graphql:208` already selects
    `review { id submittedAt author { __typename … } }`, so denormalize
    `dismissed_review_author_type` (and `_node_id`) into `extra` and pass them
    through to the synthesized row. Rows whose `extra` predates the new keys
    stay NULL.
  - Historical: the `nodes(ids:)` backfill already covers them, because the
    synthesized row's `github_node_id` *is* the review's node id.
- **`actor_login = ""` is not a reliable "deleted account" marker today**, so
  do not lean on it. The Labeled/Unlabeled/Assigned/Unassigned/ReadyForReview/
  ConvertToDraft/Reopened/Closed/ForcePushed branches write `None` for a null
  actor (raw `.get("login")`), while IssueComment/ReviewDismissed/
  ReviewRequested/PullRequestReview write `""` via `_login_or_empty`. The
  `actor_type` NULL-means-unknown convention stands on its own and does not
  depend on resolving that inconsistency.
- **Archive-import rows have no actor at all, not merely no typename.** The
  legacy fragment `src/queueboard/queries/pr_info.graphql:212-345` omits the
  `actor` field entirely for `LabeledEvent`, `UnlabeledEvent`,
  `AssignedEvent`, `ClosedEvent`, `ReopenedEvent`, `HeadRefForcePushedEvent`,
  `ReadyForReviewEvent`, and `ConvertToDraftEvent` — so those rows have NULL
  `actor_login` today, not just NULL `actor_type`. The `nodes(ids:)` backfill
  re-resolves all three from GitHub, so it heals them (in scope, per Goals).

  Note there is nothing to fix *in the query*: `pr_info.graphql` is not loaded
  by any code in this repo (the only remaining reference is
  `scripts/basic_pr_info.sh`, which uses the `basic_pr_info.graphql` subset).
  The archive JSON is already-captured output produced outside this repo, so
  the file is documentation of a historical shape, not a live fetch path.
  Widening it would change nothing. The backfill is the only available fix,
  which is part of why folding it in here is worth it even though the importer
  is not expected to run again.
- `_extract_event_fields` is the **single funnel** for every ingest path —
  verified: `sync_timeline_events(` has five call sites
  (`pr_sync_service.py:166,471,508`, `sync_tasks.py:274`,
  `archive_import.py:328`) and all route through `timeline_sync.py:316`. So
  unlike the sub-sync wire-up hazard called out in design doc 044
  ("three-call-site rule"), one extraction change covers every path. Confirmed
  there is no separate extraction in `sync_schema_upgrade_v2.py` / `_v3.py`.
- `PullRequestReview` and `IssueComment` carry `author`, not `actor`; the
  other branches carry `actor`. Both selection sets already include the
  typename union.
- **New query files must be registered with the validator.**
  `scripts/validate_github_graphql.py` uses an explicit list of query paths
  (`:167-214`), not a glob, and runs inside `scripts/repo_check_compose.sh`.
  A new `.graphql` file that is not added there is silently unvalidated.
- **Tunables: CLI args and module constants, not settings.** The backfill is a
  one-shot operator command, so batch size / rate floor belong on the
  argument parser with module-level constants for defaults. Per the root
  `AGENTS.md`, do **not** reach for `getattr(settings, "FOO", default)`
  without a matching `os.getenv` line in `base.py` — that phantom setting can
  never be configured. If the drain ever becomes beat-driven, wire the
  settings properly through `base.py` *and* `.env.example` at that point.

## Implementation Plan (Chunks)

1. **Model + migration + wire.** `PRActorType` choices, `actor_type` and
   `actor_node_id` fields, migration
   `0054_prtimelineevent_actor_type_and_node_id.py` (latest existing is
   `0053`). Add `id` to the actor/author unions in `pr_bundle.graphql`,
   `timeline_page.graphql`, `timeline_page_back.graphql`. Update
   `qb_site/syncer/admin.py` `PRTimelineEventAdmin` per the AGENTS.md
   admin-sync rule: `actor_type` into `list_display` (`:906`) and
   `list_filter` (`:914` — a three-valued enum is a filter, not a search
   field), `actor_node_id` into `search_fields` (`:920`), and both into
   `readonly_fields` (`:935`) alongside `actor_login`.
2. **Extraction.** `_actor_type_or_none` / `_actor_node_id_or_none` helpers;
   set both fields in every branch that sets `actor_login`:
   - `LabeledEvent`, `UnlabeledEvent`
   - `AssignedEvent`, `UnassignedEvent`
   - `ReadyForReviewEvent`, `ConvertToDraftEvent`, `ReopenedEvent`, `ClosedEvent`
   - `HeadRefForcePushedEvent`
   - `IssueComment` (`author`)
   - `PullRequestReview` (`author`)
   - `ReviewDismissedEvent` (dismisser)
   - `ReviewRequestedEvent`, `ReviewRequestRemovedEvent`

   …plus `_synthesize_dismissed_review_parent` via new
   `dismissed_review_author_type` / `_node_id` keys in `extra`, and add
   `"actor_type"` and `"actor_node_id"` to the fill-empty tuple at
   `timeline_sync.py:336`.
3. **Backfill.** New query `qb_site/syncer/queries/actor_types_by_node_ids.graphql`
   (registered in `scripts/validate_github_graphql.py`) plus management
   command `backfill_timeline_actor_types`:
   - id-cursor batches over
     `PRTimelineEvent.objects.filter(actor_type__isnull=True, github_node_id__isnull=False)`,
     100 node ids per `nodes(ids:)` call;
   - `bulk_update` of `actor_type` / `actor_node_id`, plus fill-only
     `actor_login` where the stored value is NULL or `""` (archive rows);
   - pause when the cached rate snapshot is below a floor
     (`syncer/services/rate_budget.get_rate_snapshot`);
   - `--dry-run` reporting the resolved distribution without writing,
     plus `--limit` / `--batch-size`;
   - counts and reports rows skipped for NULL `github_node_id` and nodes
     GitHub returned as null/unresolvable.
4. **Export verification.** After one export run, confirm
   `syncer_prtimelineevent.parquet` carries `actor_type` and `actor_node_id`
   **with string dtype** (not all-NaN float64), and that the known bot logins
   come back as `Bot`. Note the machine-user exceptions and the
   NULL-means-unknown invariant in the `qb-notebook` /
   `analytics-datasets` docs, and switch `DEFAULT_BOT_ACTORS` to be keyed on
   `actor_node_id`.

Follow-up deliberately out of scope: the same treatment for
`PRReviewInlineComment.author_login` — see Non-Goals for why it is
denormalization rather than new signal, and what would justify revisiting it.

## Validation Plan

- tests (`qb_site/syncer/tests/services/`, colocated with the existing
  timeline-sync coverage):
  - `_extract_event_fields` unit coverage: one case per `__typename` branch
    asserting `actor_type` / `actor_node_id` alongside `actor_login`,
    including `Bot` and `Mannequin` actors and a null/absent actor → `None`.
  - Unknown typename (e.g. a future `Organization`) → `None`, not stored raw.
  - Fill-empty path: an existing row with `actor_type=None` gets populated on
    rewalk; an existing non-null `actor_type` is **not** overwritten.
  - Synthesized dismissed-review parent picks up the type/node id when
    `extra` carries the new keys, and stays `None` when it does not.
  - Archive mode: rows ingest fine with `actor_type=None`.
  - Backfill command: idempotent; leaves already-populated rows untouched;
    handles a `nodes` response containing `null` entries; batches at the
    100-id cap.
  - Backfill `actor_login` fill-only behavior: a row with NULL `actor_login`
    and one with `""` both get populated; a row with an existing login is
    **not** overwritten even when GitHub now reports a different (renamed)
    login for the same account.
- manual checks:
  - Re-measure `rateLimit.cost` for a full 100-id `nodes(ids:)` call before
    starting the drain, and confirm the ~6 k-point estimate.
  - Post-backfill, `SELECT actor_login, actor_type, count(*) … GROUP BY 1,2`
    should show `Bot` for `github-actions`, `mathlib-bors`, `bors`,
    `mathlib-dependent-issues`, `mathlib-merge-conflicts`, `mathlib-auto-merge`,
    `mergify`, and `User` for the machine accounts noted above.
  - Cross-check a handful of rows against the live GraphQL response.
  - Post-deploy canary (per the syncer AGENTS.md ingestion checklist): track
    `SELECT count(*) FROM syncer_prtimelineevent WHERE actor_type IS NULL`
    per repo. It should fall steeply during the drain and then plateau at the
    genuinely-null-actor population — a plateau at the *starting* value means
    the fill-empty allowlist was missed.
  - Confirm the `(actor_login, actor_node_id)` pairs reproduce the known
    2026-02-03 renames (same node id, two logins).
  - Archive rows: `SELECT count(*) FROM syncer_prtimelineevent WHERE
    archive_imported_at IS NOT NULL AND coalesce(actor_login, '') = ''`
    should fall substantially during the drain, bottoming out at the
    genuinely-null-actor share rather than at zero.

## Alternatives Considered

- **Login → type map resolved from the API** (the original draft's
  recommendation). Rejected on two counts. First, the stated mechanism does
  not exist: GitHub's GraphQL has no `bot(login:)` root field, and
  `user(login:)` / `repositoryOwner(login:)` cannot return a `Bot`, so "Bot"
  could only be inferred from lookup *failure* — which conflates bots with
  deleted accounts, organizations, and mannequins, the opposite of the
  exactness this doc wants. REST (`/users/<slug>[bot]` → `type: "Bot"`) can do
  it, but `github_client.py:66` is GraphQL-only, so that is new plumbing.
  Second, and fatally, a map resolved *today* is blind to the retired logins
  (`mathlib4-merge-conflict-bot`, `leanprover-community-mathlib4-bot`) that
  motivated the whole exercise.
- **Schema-version wave** (`CURRENT_SYNC_SCHEMA_VERSION = 4`, `UpgradeToV4`,
  a `0055` reset migration mirroring `0045`). Correct and exact, and cheap to
  *write* — `UpgradeToV3` is `class UpgradeToV3(UpgradeToV2): version = 3`
  (`sync_schema_upgrade_v3.py:38-47`) plus a 25-line reset, and the machinery
  is proven (v3 drained in <24 h). Kept **in reserve**: the targeted
  `nodes(ids:)` route gets the same exactness for roughly 1/20th of the rate
  budget, needs no reset migration, and does not stall or interact with the
  upgrader chain — and it heals the archive rows' `actor_login` just as well,
  so that is no longer a differentiator. Fall back to the wave if
  `nodes(ids:)` turns out to be rate-limited differently at 100 ids than the
  probe suggests, or if it proves unable to resolve some class of stored node
  id. The wave's one genuine advantage is that it re-ingests *everything*, so
  it would also pick up any other field the legacy archive fragment omitted —
  worth remembering if a second archive-shaped gap ever surfaces.
- **A normalized actor-directory table** (login or node id → type), rather
  than denormalizing onto ~600 k event rows. Attractive because account kind
  really is a property of the account. Rejected because the historical rows
  still need per-event resolution to be typed at all (the retired logins are
  only recoverable through the events that reference them), so the directory
  would not avoid the expensive part — and a denormalized column needs no
  join in the parquet export, which is the only consumer.

## Progress Notes

- 2026-08-12: Drafted. Not started. Motivating investigation and all measured
  numbers above come from a `qb-notebook` session; the stale-list symptom was
  patched downstream by extending `DEFAULT_BOT_ACTORS`, which is a stopgap —
  it will go stale again at the next rename. That patch is the reason this is
  worth doing, not a substitute for it.
- 2026-08-13: Reviewed against the tree. Claims about the three live queries,
  the single extraction funnel, the fill-empty allowlist, the `SELECT *`
  export, sanitization, and migration `0053` all verified. Revisions:
  - Backfill route changed from a login→type map to targeted `nodes(ids:)`
    resolution, after live probes showed the map's mechanism does not exist in
    GraphQL and the node route is both exact and ~20× cheaper than a wave.
    Wave demoted to a recorded fallback.
  - `actor_node_id` added to scope: `actor_type` alone leaves the
    machine-user half of the downstream list login-keyed, so it does not
    actually fix the motivating rename bug.
  - `_synthesize_dismissed_review_parent` added to the extraction checklist —
    it was missing, and it produces exactly the review rows the downstream
    metric reads.
  - Dropped `db_index=True` (three-valued column, no in-repo consumer, and
    the export reads a flat parquet).
  - Added the parquet dtype-drift trap, the empirical null-actor rate, the
    GraphQL-validator registration step, and the AGENTS.md settings-hygiene
    note. Corrected the `node_kind`, `extra`-is-`{}`, `[bot]`-suffix, and
    `actor_login = ""` claims.
- 2026-08-13 (follow-up): archive-row `actor_login` healing **promoted from
  follow-up into scope** — the backfill resolves those nodes anyway, and it is
  the only possible fix since `pr_info.graphql` is no longer a live fetch path
  in this repo. Fill is guarded fill-only so ingest-time logins and the rename
  history survive. `PRReviewInlineComment.author_login` stays out of scope,
  with the reasoning recorded under Non-Goals after a probe found 0 author
  mismatches against parent reviews in a 33-comment sample.

## Finalization Notes

- After Chunk 4, collapse into a final decision record: keep the machine-user
  caveat, the `NULL` ≠ `User` invariant, the key-on-`actor_node_id` guidance,
  the fill-empty-allowlist requirement, and the parquet dtype trap; drop the
  chunk sequencing, the probe transcripts, and the Alternatives Considered
  deliberation once the route has been executed. Keep the archive-row outcome
  (how much `actor_login` the backfill recovered) as a migration outcome, and
  keep the `PRReviewInlineComment.author_login` non-goal with its reasoning
  rather than letting it vanish with the plan — the next person to notice the
  asymmetry will ask.
