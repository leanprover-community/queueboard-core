# Timeline Actor Type for Bot Classification

> Status: **Accepted** — implemented in PR #194 (`doc/051-timeline-actor-type`).
> The production drain, the first post-drain export, and the downstream switch
> in `qb-notebook` are still outstanding; see Operational Notes.
> Converted from a living implementation plan to a decision record on
> 2026-08-23 (plan history is in git).

## Context

- `PRTimelineEvent.actor_login` stored *who* acted, but not *what kind of
  account* acted, and not a rename-stable identity for that account. Every
  downstream consumer that needed "was this a human?" therefore hardcoded a
  list of bot logins — and **login is the wrong key**, which is the root cause:
  any identity change silently invalidates the list.
- GitHub already told us, we already asked, and we threw it away. All three
  live timeline queries selected the actor's `__typename`
  (`pr_bundle.graphql`, `timeline_page.graphql`, `timeline_page_back.graphql`);
  `_login_or_empty()` in `syncer/services/sub/timeline_sync.py` kept only
  `login` and dropped it.
- **Motivating failure.** `qb-notebook`'s `DEFAULT_BOT_ACTORS`
  (`qb_notebook/review_states.py`) finds each PR's first *human* review touch.
  The mathlib bots changed identity on **2026-02-03** and the list went stale
  unnoticed. Effect on "open → first review" for a last-90-days cohort
  (snapshot 2026-08-10): **15.7 % of first touches were bot events**, and the
  median moved 0.261 d → 0.467 d once filtered — a reported ~41 % latency
  improvement that was mostly an artifact. Six months of a wrong number from a
  change nobody downstream could have seen.
- **What the changeover actually was** (resolved live 2026-08-15, correcting
  the original "rename" reading):

  | login | kind | node id |
  | --- | --- | --- |
  | `mathlib4-merge-conflict-bot` | `User` | `U_kgDODVl3LA` |
  | `mathlib-merge-conflicts` | **`Bot`** | `BOT_kgDOD2_IkQ` |
  | `mathlib4-dependent-issues-bot` | `User` | `U_kgDOCsITAQ` |
  | `mathlib-dependent-issues` | **`Bot`** | `BOT_kgDOD2_cBQ` |

  The machine-user bots were **replaced by GitHub Apps** — not renamed. The old
  logins still resolve to their original accounts. Consequences: `actor_type`
  alone catches the replacements and any future App with no list to update; no
  key, node id included, could have bridged the substitution, so
  `actor_node_id` is justified by the ordinary renames it *does* survive and by
  giving the residual machine-user list a stable key. That residual list is the
  old, frozen accounts (`U_kgDODVl3LA`, `U_kgDOCsITAQ`, and
  `leanprover-community-bot-assistant` = `U_kgDOBcsTTQ`), plus
  `leanprover-radar` (`User`, `U_kgDOCG88RQ`); `mathlib-triage` is a `Bot`
  (`BOT_kgDOD2_uYQ`).
- Automation accounts with no API-side bot signal at all, found in the same
  sweep: `mathlib-auto-merge`, `mathlib-splicebot`, `leanprover-bot`,
  `mergify`, `downstream-reports-automation`, `botbaki-review`,
  `copilot-pull-request-reviewer`, `copilot-swe-agent`.
- No usable substitute existed in the export:
  - `core_user.github_node_id` encodes the kind in its prefix, but `core_user`
    is a PR-**author** table: 60 event actors accounting for **111,864 touch
    events (~41 %)** have no row there, because most bots never open PRs.
  - The GitHub App `[bot]` login suffix is not available: **0** occurrences
    anywhere in the export. GraphQL's `Bot.login` never includes it (REST's
    `/users/<slug>[bot]` does), so this is not something we strip.
  - `PRTimelineEvent.extra` is populated only for `REVIEW_DISMISSED` rows and
    arrives in parquet as a JSON *string*; not a substitute for a typed column.
- Precedent inside the same function: the `ReviewRequested` /
  `ReviewRequestRemoved` branch already read `requestedReviewer.__typename` to
  route `User`/`Bot`/`Mannequin` vs `Team`, and the top-level `author`
  selections already requested `id`. The timeline actor unions just never did.

## Decision

- **Columns.** `PRTimelineEvent` gains `actor_type` (`PRActorType` choices
  `User` / `Bot` / `Mannequin`, GitHub's exact wire casing) and
  `actor_node_id`, both nullable — migration
  `syncer/migrations/0054_prtimelineevent_actor_type_and_node_id.py`. `NULL`
  means *unknown*, never `User`.
  - **No index on either.** `actor_type` is three-valued, the "first human
    touch per PR" shape is already served by `syncer_prtimeline_pr_time_idx`,
    and the analytics consumer reads a `SELECT *` parquet. Add one when an
    in-repo query needs it.
  - **No CHECK constraint.** Unlike `before_sha` / `label_name` /
    `requested_*`, these are meaningful on every event type, so there is
    nothing to type-scope. In particular, do not encode "`actor_type` set ⟹
    `actor_login` non-empty": it holds today but is a GitHub-side invariant we
    do not control.
- **Wire.** `id` added to the 12 timeline `actor` unions, the `IssueComment` /
  `PullRequestReview` `author` unions, and `ReviewDismissedEvent`'s
  `review { author }` in all three live queries. Adding fields to an existing
  selection does not change GraphQL point cost. The inline-comment `author`
  under `PullRequestReview.comments.nodes` is deliberately left `login`-only
  (see the non-goal below).
- **Extraction.** `actor_type_or_none` / `actor_node_id_or_none` (public,
  because the backfill command imports them) plus a thin `_actor_identity`
  pair-builder in `timeline_sync.py`, applied in every branch of
  `_extract_event_fields` that sets `actor_login` — both `actor` idioms and the
  `author` branches. The allowed typename set is derived from
  `PRActorType.values` so the helper cannot drift from the model. An
  *unmodelled* typename (a hypothetical `Organization`) yields
  `actor_type = NULL` but still stores `actor_node_id`: the node id is exact
  regardless of whether we model the kind.
  `_extract_event_fields` is the single funnel for all five
  `sync_timeline_events` call sites, so one extraction change covers every
  ingest path, archive import included.
- **Synthesized dismissed-review parents.** `REVIEW_DISMISSED` rows now
  denormalize `dismissed_review_author_type` / `_node_id` into `extra`, and
  `_synthesize_dismissed_review_parent` reads them back — otherwise those rows
  (which are exactly the review events the motivating metric reads) would be
  `NULL` even under fresh ingestion. Rows whose `extra` predates the new keys
  stay `NULL` and are healed by the backfill.
- **Backfill: targeted `nodes(ids:)` resolution, not a rewalk.** Every row
  already stores the timeline item's own `github_node_id`, and GitHub
  re-resolves that node's actor on demand.
  `syncer/queries/actor_types_by_node_ids.graphql` (registered in
  `scripts/validate_github_graphql.py`, which uses an explicit list, not a
  glob) plus `manage.py backfill_timeline_actor_types`. This is *exact*, not
  heuristic: it resolves the actual actor object attached to each specific
  event, so renamed accounts resolve correctly and login reuse cannot mis-type
  anything. Measured live: `rateLimit.cost` is **1 at the full 100-id cap**, so
  ~600 k rows ≈ **~6 k points** — roughly 1/20th of a schema-version rewalk
  wave, with no reset migration and no interaction with the upgrader chain.
  - A named `ActorIdentity` fragment keeps the 13 inline fragments readable;
    `... on Comment` is what covers `IssueComment`, `PullRequestReview`, and
    the synthesized dismissed-review parents (whose stored node id *is* the
    review's).
  - Per-repository clients and an outer repo loop, because a single cross-repo
    client would bypass GitHub App operation-token resolution that every other
    syncer entry point goes through.
- **Archive-row `actor_login` healing, in the same pass.** The legacy archive
  fragment omitted the `actor` field entirely for eight event types, so those
  rows have no attribution at all — not merely no typename. The same
  `nodes(ids:)` response carries `login`, so the drain fills it, guarded
  **fill-only**: write only when the resolved login is non-empty *and* the
  stored value is empty, with the predicate covering both `NULL` and `''`
  because the two extraction idioms disagree. A non-empty stored login is the
  login *as of ingest time*; clobbering it with today's login would destroy the
  rename history `actor_node_id` exists to expose. (Nothing to fix in
  `src/queueboard/queries/pr_info.graphql`: it is no longer a live fetch path,
  so the backfill is the only available remedy.)
- **Fill-empty allowlist extended.** `actor_type` and `actor_node_id` are added
  to the explicit column tuple `sync_timeline_events` iterates when updating
  existing rows. The drain writes via `bulk_update` and does not depend on
  this, but every ordinary rewalk does — including the continuous timeline
  backfill for PRs not yet `timeline_backfill_done`. **This is the single
  easiest thing to break in any future change here**; omit it and those paths
  silently never populate the columns.
- **Export and sanitization unchanged.**
  `EXPORT_TABLE_QUERIES["syncer_prtimelineevent"]` is `SELECT *`, so the
  columns flow through automatically; `scripts/sanitize_backup.py` references
  neither the table nor any `actor_*` column. `actor_type` is a three-valued
  enum and `actor_node_id` an opaque GitHub identifier already exported for
  authors via `core_user.github_node_id`.
- **Progress is monitored, not eyeballed.** `syncer.collect_convergence`
  records two per-repo counters on `SyncerConvergenceSnapshot` (migration
  `0055`), following the precedent of `archive_resync_remaining` for the
  doc-043 drain:
  - `timeline_events_missing_actor_type` — the backfill command's exact target
    set (`actor_type IS NULL AND github_node_id IS NOT NULL`), so the admin
    page and the command's own output agree. It **plateaus** at the null-actor
    floor rather than reaching 0.
  - `timeline_events_untyped_with_login` — the same rows narrowed to those
    carrying a login. A row with a login demonstrably had an actor, so this is
    typeable work: it converges to ~0 and then **stays** there. That makes it
    the standing regression canary — if it climbs later, ingestion stopped
    typing actors, most likely because the fill-empty allowlist was dropped.

  Both are `COUNT(*)` over `syncer_prtimelineevent` per active repo every
  `ANALYTICS_CONVERGENCE_PERIOD_SECONDS` (900 s). With no index on
  `actor_type` that is a sequential scan, which is acceptable at this table
  size and cadence; a partial index `WHERE actor_type IS NULL` is the escape
  hatch if the collector ever gets slow.
- **Not in scope: `PRReviewInlineComment.author_login`.** The mechanism would
  work verbatim (the wire already carries `author { __typename … }`,
  `github_node_id` is unique, the table is exported), but it carries **no new
  information**: an inline comment's author is the parent review's author.
  Sampled 30 recent merged mathlib4 PRs (2026-08-13): 33 inline comments, **0
  author mismatches** against their parent review — 3 were bot-authored, so
  bots do post inline comments, they just do it as their own review. So
  `author_type` is derivable by joining `review_node_id` →
  `PRTimelineEvent.github_node_id`. Treat the sample as absence-of-
  counterexample, not proof. Residual gap: comments whose parent
  `PRTimelineEvent` row does not exist (the documented null
  `parent_review_event` case) have nothing to join to. What would justify
  revisiting: per-comment review-effort metrics that count inline comments per
  author *without* joining to the parent, where
  `copilot-pull-request-reviewer` / `copilot-swe-agent` would inflate human
  review volume. Denormalization for convenience, not correctness — so it
  should be driven by a real downstream query, not by symmetry with this
  change.

## Consequences

- **`__typename == "Bot"` identifies GitHub *Apps*, not all automation.**
  Machine accounts that are ordinary user accounts report `User` — confirmed
  for `leanprover-community-bot-assistant`, `leanprover-radar`, and both
  retired mathlib bots. `actor_type` is therefore **necessary but not
  sufficient**: the downstream machine-user list shrinks and stops being
  rename-fragile, but does not disappear. Key that residual list on
  `actor_node_id`.
- **`actor_type IS NULL` is a permanent, non-trivial population.** GitHub
  returns a null actor for a real share of events: 12 of the first 100 real
  mathlib4 nodes probed, concentrated in workflow-driven label events
  (`delegated`, `ready-to-merge`). No backfill route can type these. Analysts
  must read `NULL` as *unknown*, never as `User` — this repo has no export
  README, so that documentation lives in `qb-notebook` / `analytics-datasets`.
- The set of `(actor_login, actor_node_id)` pairs is a free rename history —
  and read the other way, a *changed* node id under a similar-looking login is
  the signature of an account replacement like the 2026-02-03 one.
- The update path is fill-only (`new_val and not getattr(obj, col)`), so a
  rewalk never overwrites an existing `actor_type`. That makes the drain
  idempotent and order-independent with respect to the live syncer.
- Synthesis uses `get_or_create`, so a synthesized parent row that already
  exists from a pre-051 ingest is **not** retyped by a later dismiss-event
  re-ingest. Those rows are healed by the ordinary update path if the
  `PullRequestReview` node itself is walked, and by the drain otherwise. Left
  as-is rather than special-cased: the fill-only convention stays uniform.
- **Parquet dtype drift.** `scripts/export_for_analysis.py` goes
  Postgres → `COPY … CSV` → `pd.read_csv` → `to_parquet`. An export run while a
  column is entirely NULL infers all-NaN **float64** and lands as `double`; a
  later export lands as `object`. `requested_team_slug` in the current artifact
  is `double` for exactly this reason. Hence the export must be sequenced
  *after* the drain, and the check is on dtype, not just column presence.
- Width cost: ~40 B × ~600 k rows on two columns, in the table and in the
  export. `actor_node_id` is the separable half if that is ever unwelcome —
  dropping it would mean keeping the rename fragility downstream.
- Tunables are CLI args plus module constants, deliberately not settings: a
  one-shot operator command must not introduce a `getattr(settings, …)` with no
  matching `os.getenv` in `base.py` (root `AGENTS.md`). If the drain ever
  becomes beat-driven, wire it through `base.py` *and* `.env.example` then.

## Operational Notes

### Status

- Landed in PR #194: columns + migration `0054` + admin
  (`actor_type` in `list_display`/`list_filter`, `actor_node_id` in
  `search_fields`, both in `readonly_fields`), extraction, and the backfill
  command with 22 + 23 tests, and the two convergence counters (migration
  `0055`) with 3 tests. Full `syncer` suite green (527 tests) on
  host-against-dockerized-Postgres; `scripts/validate_github_graphql.py` and
  `scripts/validate_backup_policy.py` pass; CI `checks` + `docker` (which runs
  `scripts/repo_check_compose.sh`) green.
- Validated against the live API, not only fakes: 154 real mathlib4 node ids
  resolved through the new query, every actor typed as expected, 12 null
  actors in the first 100, 0 unresolvable, and cost 1 at the 100-id cap.
- Outstanding: the drain, the first post-drain export, and the `qb-notebook`
  switch.

### No new configuration

The command adds **no settings and no env vars**. It uses the same token path
as the live syncer — `GitHubClient(operation="syncer_pr_read", owner=…,
repo=…)`, i.e. the GitHub App installation token, falling back to
`GH_TOKEN`/`GITHUB_TOKEN` — and reads the rate snapshot from the Redis behind
`CELERY_BROKER_URL`, which the worker already uses. If Redis is unreachable the
snapshot is `None`, the rate gate silently does nothing, and the drain instead
stops on GitHub's own rejection (below). Request pacing comes from the existing
`SYNCER_GH_THROTTLE_MS` (250 ms), shared cross-process with the live syncer.

### Runbook

1. **Deploy.** Merge and push; the Procfile `release` phase runs `migrate`.
   Both columns are nullable with no default, so `0054` is a metadata-only
   `ALTER TABLE` — no rewrite and no long lock on ~600 k rows. From here the
   live syncer types new events, and rewalks heal old rows through the
   fill-empty allowlist.
2. **Dry run, to see the distribution before writing anything.** It resolves
   for real and only skips the writes, so it **spends the same GraphQL points**
   as a live run of the same size — which is what makes it a usable cost probe.
   Heroku consumes `--flags` before passthrough, so keep the `--`, and set
   `PYTHONPATH` the way the Procfile does:

   ```bash
   heroku run --app queueboard-backend --no-tty -- \
     sh -c 'export PYTHONPATH=$PWD/qb_site:$PWD${PYTHONPATH:+:$PYTHONPATH}; \
            exec python qb_site/manage.py backfill_timeline_actor_types \
              --repo leanprover-community/mathlib4 --dry-run --limit 2000'
   ```
3. **Drain.** ~6 k points against a 5 000/hr budget *shared with the live
   syncer*, plus 250 ms throttle per call — expect a few hours and at least one
   sleep through a rate reset. Use a detached dyno so a dropped connection does
   not kill it:

   ```bash
   heroku run:detached --app queueboard-backend --size=standard-1x -- \
     sh -c 'export PYTHONPATH=$PWD/qb_site:$PWD${PYTHONPATH:+:$PYTHONPATH}; \
            exec python qb_site/manage.py backfill_timeline_actor_types --wait-for-rate'
   heroku logs --app queueboard-backend --dyno run.NNNN --tail
   ```

   Without `--wait-for-rate` the drain stops cleanly at the floor
   (`--min-rate-remaining`, default 500) and is resumable — the target set is
   just `actor_type IS NULL`. `--limit` / `--batch-size` / `--repo` drip-feed it
   under supervision.
4. **Read the counters.** Per repo and in total:
   `scanned`, `written`, `typed`, `node_ids`, `logins`, then the four ways a row
   can come back untyped, which mean different things and must not be conflated:
   - `null_actor` — GitHub says there is no actor. Permanent; this is the floor
     the drain plateaus at.
   - `unresolved` — the node id no longer resolves (hard-deleted comment or
     review). Also permanent.
   - `call_failed` — the call failed for a reason unrelated to the row
     (transport error, or a GraphQL error not attributable to an id). Retried
     up to 3 times with backoff first; whatever is left is **not** a fact about
     the row, and a later run picks it up.
   - `unmodelled` — a typename outside `PRActorType`. The node id is still
     stored.

   `points` is the summed `rateLimit.cost` from the responses themselves —
   measured spend, not `api_calls` × an assumed price — and the closing `rate:`
   line reports the `remaining` / `used` / `resetAt` the run leaves behind. A
   small `--limit` run is therefore a direct cost probe: extrapolate from its
   `points` / `scanned` ratio before committing to the full drain.

   A rate-limit rejection from GitHub unwinds to the same resumable stop as the
   floor rather than retrying or splitting, since both would only spend a
   budget that is already gone. `retries` counts re-attempts, so a run fighting
   a flaky API is visible.
5. **Watch the canaries** (per the syncer `AGENTS.md` ingestion checklist).
   The first two are on the `SyncerConvergenceSnapshot` admin change list,
   refreshed every 15 minutes, so the drain can be followed there rather than
   by hand — but the collector only starts recording them from the deploy that
   carries `0055`, so the first snapshot is the baseline:
   - `timeline_events_missing_actor_type` per repo. Should fall steeply, then
     plateau at the genuinely-null-actor population. **A plateau at the
     *starting* value means the fill-empty allowlist regressed.** Equivalent
     SQL: `SELECT count(*) FROM syncer_prtimelineevent WHERE actor_type IS
     NULL AND github_node_id IS NOT NULL`.
   - `timeline_events_untyped_with_login` per repo — the converging half.
     Should approach 0; whatever remains is unresolved nodes and unmodelled
     typenames, which the command's counters name explicitly.
   - `SELECT count(*) FROM syncer_prtimelineevent WHERE archive_imported_at IS
     NOT NULL AND coalesce(actor_login, '') = ''` — should fall substantially,
     bottoming out at the null-actor share rather than at zero.
   - `SELECT actor_login, actor_type, count(*) … GROUP BY 1,2` should show
     `Bot` for `github-actions`, `mathlib-bors`, `mathlib-dependent-issues`,
     `mathlib-merge-conflicts`, `mathlib-triage`, and `User` for the machine
     accounts — **including** the two retired `mathlib4-*-bot` logins, which
     are machine users, not Apps. Do not expect those to come back as `Bot`.
   - The `(actor_login, actor_node_id)` pairs should reproduce 2026-02-03 as a
     replacement: retired `mathlib4-*` logins keep their `U_…` ids and stop
     after 2026-02-02; `mathlib-*` logins appear from 2026-02-03 with fresh
     `BOT_…` ids.
6. **Only then export** (`upload_backup.yaml`, daily 06:00 UTC or on demand),
   so the first parquet is written with values present and pandas infers
   `object`. Verify dtype, not just presence.
7. **Only then switch downstream.** In `qb-notebook`: key the residual
   automation list on `actor_node_id`, drop the `Bot`-typed accounts from it,
   and document the `NULL` ≠ `User` invariant plus the machine-user caveat in
   `docs/schema-notes.md`. Note that rows with `actor_type IS NULL` still need
   the login list, so it shrinks rather than disappears.

### Fallback

If `nodes(ids:)` ever proves unable to resolve some class of stored node id, the
schema-version wave remains available and cheap to write
(`CURRENT_SYNC_SCHEMA_VERSION = 4`, `UpgradeToV4` subclassing `UpgradeToV3`, a
reset migration mirroring `0045`); v3 drained in under 24 h. Its one genuine
advantage is that it re-ingests *everything*, so it would also pick up any
other field the legacy archive fragment omitted.

## Alternatives

- **Login → type map resolved from the API.** Rejected: the mechanism does not
  exist. GraphQL has no `bot(login:)` root field, and `user(login:)` /
  `repositoryOwner(login:)` cannot return a `Bot` — confirmed live, where
  `repositoryOwner(login: "mathlib-merge-conflicts")` returns `null` precisely
  *because* that account is a `Bot`. "Bot" could only be inferred from lookup
  failure, conflating bots with deleted accounts, organizations, and
  mannequins. REST could do it, but `github_client.py` is GraphQL-only. Fatally,
  a map resolved *today* is blind to the retired logins that motivated the
  exercise.
- **Schema-version wave.** Correct and exact, but ~20× the rate budget, needs a
  reset migration, and stalls behind the upgrader chain. Kept in reserve
  (above).
- **A normalized actor-directory table** (login or node id → type) instead of
  denormalizing onto ~600 k rows. Attractive, since account kind really is a
  property of the account. Rejected because the historical rows still need
  per-event resolution to be typed at all — the retired logins are only
  recoverable through the events that reference them — so the directory would
  not avoid the expensive part, and a denormalized column needs no join in the
  parquet export, which is the only consumer.
