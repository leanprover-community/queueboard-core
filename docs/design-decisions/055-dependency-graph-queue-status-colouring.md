# 055 — Colour the dependency graph by queue status, not by dependency count

## Context

- `dependency_dashboard.html` coloured nodes by *whether a PR has dependencies* (green = none,
  blue = some, yellow = draft). That is already encoded by the graph's own edges, so the fill
  channel carried no information a reader could not see, while the thing a reviewer actually
  wants — is this reviewable right now, and if not, who has the ball — was absent.
- The graph payload also flattened labels to bare names, dropping the colours the snapshot
  already carries, so the graph's labels looked nothing like the queueboard's or GitHub's.
- Two fields could drive a status colour and they do not agree:
  - `pr_status` (`PRStatus`), classified from labels + CI + draft + fork-ness;
  - queue membership, `snapshot.lists.dashboards.Queue`, which additionally applies the
    base-branch check, the rule set's required/forbidden labels and CI gating.
  A PR can be `AwaitingReview` and still not be on the queue (wrong base branch, failing
  required CI, a forbidden label).
- `PRStatus.NotFromFork` short-circuits `determine_PR_status` before it looks at labels or CI.
  In mathlib most contributors push branches to the main repository rather than a fork, so this
  is not a rare edge: in a recent snapshot it was ~40% of all open PRs (410 of 2101 would have
  been left unclassified). Colouring those by `pr_status` alone paints a large flat blob whose
  members are really drafts, blocked PRs and PRs awaiting their author.
- `maintainer-merge` is a plain GitHub label that does **not** appear in
  `label_categorisation_rules`, so it does not affect `pr_status` at all, and it is not a
  forbidden queue label. In the same snapshot, 17 of the 18 such PRs were on the review queue —
  yet `QueueRuleSet.assignment_forbidden_label_names` already documents them as a "post-review"
  signal "which a reviewer can take no further action on".

## Decision

- **Fill encodes queue status; a ring encodes blockedness.** The node's fill comes from
  `nodeCategory(node)`; a separate ring *outside* the node, not a stroke on it, marks
  `dependency_count > 0` — an inner stroke eats the fill it qualifies and can only contrast
  with that fill (~2:1), where the same colour clears 10:1 against the page. Two questions,
  two channels, both readable at once.
- **Buckets answer "who has the ball?", they do not mirror `PRStatus` one-for-one.** Nine hues
  are more than colour carries at a glance, and several `PRStatus` values differ only in ways
  that do not change who acts next. Five working buckets, plus one that should stay empty:

  | bucket | covers | connected subgraph |
  | --- | --- | --- |
  | On the review queue | `on_queue`, `AwaitingReview` | 117 |
  | Waiting on the author | `AwaitingAuthor`, `MergeConflict`, `HelpWanted` | 93 |
  | Blocked | `Blocked`, `AwaitingDecision` | 411 |
  | Not ready | `NotReady` (draft, WIP, awaiting-CI, CI not passing) | 68 |
  | Approved, awaiting merge | `maintainer-merge`, `Delegated`, `AwaitingBors` | 6 |
  | Conflicting signals | `Contradictory` | 2 |

- **Order of precedence: maintainer-merge, then queue membership, then status.**
  - `maintainer-merge` is checked first. Those PRs are on the queue but already reviewed, so
    painting them green would send reviewers at work that is not theirs. They are read from
    `snapshot.lists.dashboards.AllMaintainerMerge`, which already excludes the ones that also
    carry `ready-to-merge` (those are `AwaitingBors`, and land in the same bucket anyway).
    A separate sixth colour was considered and rejected: it would have split a 6-PR bucket into
    5 and 1. Maintainers who want just those PRs can use the label facet filter, or
    `maintainers_quick.html`, which lists them properly.
  - `on_queue` next, because it is the only field that has applied every gate.
- **`NotFromFork` is resolved by re-running the real classifier, not by guessing.** Both
  producers emit `pr_status_ignoring_fork`, computed by
  `classify_pr_state.determine_PR_status_ignoring_fork` — the same `determine_PR_status`, with
  `from_fork=True` forced. This is what the graph colours by. Guessing from `is_draft` /
  `ci_status` / `dependency_count` instead was tried and left 44 of 697 connected PRs
  unclassified while their labels plainly said `awaiting-author` or `merge-conflict`; the
  label precedence rules live in `determine_PR_status` and reimplementing them in JS would
  drift. Reusing the classifier leaves 3 PRs unclassified in the whole snapshot, all genuinely
  `Contradictory`.
- **"Conflicting signals" is not the same as the `ContradictoryLabels` dashboard.**
  `determine_PR_status` synthesises a `WIP` label kind from *draft or non-passing CI* before
  testing for contradictions, so its `Contradictory` verdict covers a label conflicting with
  the CI state, not just two conflicting labels. In practice every case in a recent snapshot
  was `ready-to-merge` while CI was failing. `has_contradictory_labels`, which feeds the
  queueboard's `ContradictoryLabels` table, is a pure-label check that deliberately excludes
  the synthesised WIP — so it reported 0 for the same snapshot where the graph showed 3. The
  bucket is named for what it means to a reader rather than after either function.
- The palette lives in one table, `NODE_CATEGORIES`, whose entries name CSS custom properties.
  Nodes and legend both read it, so they cannot drift, and each token is redefined under
  `:root[data-theme="dark"]`.
- **Dark mode is opt-in, not driven by `prefers-color-scheme`.** The page first followed the OS
  setting, which made it the only dark surface in the queueboard: a reader following the link
  from `index.html` on a dark-mode machine watched the site invert under them. The dark palette
  is kept, but reached through a toggle in the controls row and remembered in `localStorage`
  under `queueboard-dependency-theme`; with nothing stored the page is light, like every other
  page. A tiny script in `<head>` applies the stored choice before the first paint. This matches
  the standing rule for the Django reviewer pages (`qb_site/console/AGENTS.md`: the shared style
  system is "intentionally light-only"). When the frontend gains a dark mode generally, the
  default should move back to the OS preference and the storage key should become site-wide.
- The graph payload gains `pr_status`, `pr_status_ignoring_fork`, `ci_status`, `on_queue`,
  `awaiting_maintainer_merge`, `upstream_count`, `downstream_count`, and full
  `{name, color, url}` label objects. Both producers
  (`analyzer/services/dependency_graph.py` and `dashboard_data.generate_dependency_graph`)
  emit the same shape.

## Consequences

- The graph now depends on queue-rule-set state, not just PR metadata: a snapshot built with a
  different rule set colours differently. That is intended — it is the same notion of "on the
  queue" the review dashboard uses.
- `labels` changed from `list[str]` to `list[object]`. The frontend accepts both so a stale
  `dependency_graph.json` still renders, but any other consumer needs the same tolerance.
- The ring claims the node-outline channel for one meaning. Any later node-level state needs a
  different channel, or it reads as a variant of "blocked": the hover emphasis marks the edges
  incident to the hovered PR rather than outlining its neighbours, for exactly this reason.
- Grouping loses detail the tooltip has to make up for: it shows the bucket, the raw
  `pr_status` beside it, and a note when a queued PR is labelled `maintainer-merge`. Regrouping
  is an edit to `NODE_CATEGORIES` plus `nodeCategory`, with no other code change.
- `pr_status_ignoring_fork` is computed per graph build rather than stored in the snapshot, so
  the snapshot contract is unchanged — at the cost of the graph builders depending on
  `classify_pr_state`. The Django builder already reaches into `queueboard.*` for
  `CIStatus`/`determine_PR_status`, so this adds no new coupling direction.
- If `NotFromFork` ever stops being the right classification for a repo where branch PRs are
  normal, the fix belongs in `determine_PR_status`, and `determine_PR_status_ignoring_fork`
  would become redundant rather than wrong.
- Transitive counts are computed per graph build. `transitive_dependency_counts`
  (`src/queueboard/util.py`) is BFS-per-node, which is fine at mathlib's scale (~2k PRs, <1k
  edges) and is shared with the queueboard's "blocks" column so the two always agree.

## Operational Notes

- No migration and no new setting.
- `dependency_dashboard.html?focus=<pr>` renders one PR's connected component; the queueboard's
  "blocks" column links to it. A `focus` that is not in the snapshot falls back to the full
  graph and says so in the stats line, rather than rendering blank.

## Alternatives

- *Colour by `pr_status` only* — simpler and identical in both pipelines, but `AwaitingReview`
  does not mean reviewable, and `NotFromFork` swallows the answer for a large minority of PRs.
- *One hue per `PRStatus` (nine buckets)* — most faithful to the data model, but four of the
  nine held a handful of PRs each and the colours stopped being separable at a glance.
- *Two-tone, on-queue versus not* — unambiguous, but throws away the why-not, which is most of
  what a triager is looking for.
- *Add a fork-blind status to the snapshot payload* — would make it available to every
  consumer, but it is a presentational distinction and the graph builders can recompute it
  from labels + CI + draft, which the payload already carries.
