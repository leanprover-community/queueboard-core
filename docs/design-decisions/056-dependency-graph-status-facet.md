# 056 — The dependency graph's legend is a facet over chains, not a switch that hides PRs

## Context

- `dependency_dashboard.html` colours nodes by queue status (see
  `055-dependency-graph-queue-status-colouring.md`), but the legend was inert: the colours
  could be read and not acted on. The page already had two facets — labels and authors — whose
  semantics are "a node *matches* if it passes every facet, and every connected component
  containing a match is shown whole, with the matches haloed".
- The obvious reading of a clickable legend is "click to hide these PRs", and that was built
  first, at node level. Measured against `test/pre-api-artifact/api/dependency_graph.json`
  (2101 PRs, of which 697 sit in 209 components; median component 2 PRs, largest 27), it does
  not survive contact with the data:

  | bucket | PRs | hiding them strands | left on screen |
  | --- | --- | --- | --- |
  | Blocked | 411 | 279 more PRs | 7 PRs, 4 links |

  Being in a chain is close to what *makes* a PR blocked, so hiding the bucket a reader would
  most want gone deletes the graph. Keeping the stranded PRs instead gives 286 PRs and 4 links
  — a field of dots.
- Lifting "hide" to the component level fails in both directions, for the same reason.
  **"Blocked" occurs in 208 of the 209 components.**

  | unticking one bucket means… | blocked | author | not ready | queue |
  | --- | --- | --- | --- | --- |
  | drop components *containing* one | 208 drop, **1 left** | 77 drop | 64 drop | 90 drop |
  | drop components made *entirely* of it | 3 drop | 0 | 0 | 0 |

  The first is a wipe, the second a no-op.
- The positive direction does carry information. Selecting a single bucket, label-facet style:

  | show components containing ≥1… | components | PRs |
  | --- | --- | --- |
  | On the review queue | 90 of 209 | 337 |
  | Waiting on the author | 77 | 316 |
  | Not ready | 64 | 230 |
  | Approved, awaiting merge | 6 | 22 |
  | Conflicting signals | 2 | 6 |
  | Blocked | 208 | 695 (no-op) |

- 116 of the 117 queue PRs in the graph have no open blocker, so "components containing a PR
  on the review queue" is in practice "chains whose root is reviewable now" — the actionable
  triage question — without a second notion of "root" having to be defined.

## Decision

- **`currentFilter.statuses` is a facet, exactly like `labels` and `authors`**: `null` means no
  restriction, a non-empty set seeds, components containing a seed are shown whole, and it ANDs
  with the other facets, the search box and `?focus=`. Serialised as `?status=queue,merging` in
  `NODE_CATEGORIES` order.
- **A click selects rather than removes.** With six buckets, removing one leaves a component out
  only when every PR in it is in that bucket — 3 components of 209 — so five clicks in six would
  look like nothing happened. A plain click shows only the chains containing that bucket,
  clicking the selected row again clears the facet, and shift/⌘/ctrl-click adds a second bucket.
  Deselecting the last bucket means "no restriction", never "match nothing".
- **Only the selected rows are marked** (tinted, bold, a tick). The unselected rows are left
  alone rather than greyed or struck through: selecting "On the review queue" still puts 192
  blocked PRs on screen, because they are what those chains are made of, so dimming their legend
  entry would be false.
- **The status facet does not halo its matches.** A node's fill already states its bucket; the
  dashed halo exists so that a label or author match — invisible on the graph — can be told from
  the component dragged in around it. Halo marking therefore keys off the search box and the
  label/author facets only.
- **The legend's counts are of the PRs currently shown**, headed `PRs shown`, so after selecting
  a bucket the panel reports what the chains are made of (queue 117 / author 16 / blocked 192 /
  not ready 9 / merging 3 for `?status=queue`).
- **The stats line states the scope of each figure and no longer duplicates the legend.** It
  reads `In this snapshot: … 403 on the review queue | Showing: 697 PRs, 673 dependencies`. It
  used to end `… 122 on the queue`, counting raw `on_queue` over the view, which disagreed with
  the legend's `On the review queue: 117`: the bucket excludes queued PRs already labelled
  `maintainer-merge` (all 5 of the difference). Two numbers under one name, differing by a rule
  the reader cannot see, so the duplicate was removed rather than annotated.

## Consequences

- **There is no way to declutter.** Nothing hides the 411 blocked PRs; the facet picks chains,
  it does not remove nodes. That is the cost of every component staying whole, and it is the
  behaviour the label facet has always had.
- A bucket can show a count of 0 in the current view and still be worth clicking, because the
  count describes the view and the click re-seeds from the whole snapshot.
- Adding a bucket to `NODE_CATEGORIES` gives it a legend row, a count and a URL key with no
  further code. A `?status=` naming a bucket that no longer exists leaves no restriction rather
  than an empty graph.
- The legend is now interactive, so it is a click target sitting over the canvas. It was already
  registered in `overlayScreenBoxes()` for tooltip placement, which is what keeps a tooltip from
  landing on the control the reader is about to use.
- `?status=queue%2Cmerging` — `URLSearchParams` percent-encodes the comma, as it already does for
  `?labels=` and `?authors=`. Ugly in a shared link, consistent with the rest.

## Operational Notes

- No migration, no new setting, no payload change: every field the buckets read
  (`on_queue`, `awaiting_maintainer_merge`, `pr_status_ignoring_fork`) already ships.
- The measurements above are reproducible from `test/pre-api-artifact/api/dependency_graph.json`
  by applying `nodeCategory` over components of the non-singleton nodes.
- Behaviour is covered by jsdom checks over that fixture: that soloing a bucket yields exactly
  the component counts in the table, and that every component shown provably contains a PR of
  the selected bucket.

## Alternatives

- *Hide PRs at node level* — built first, and the direct reading of a legend toggle, but it cuts
  the chains that run through the hidden bucket; hiding "Blocked" leaves 7 PRs of 697.
- *Hide components containing a bucket* — a real query ("chains with nothing waiting on an
  author" is 132 of 209 components) but unusable for the bucket that matters: 1 component
  survives excluding "Blocked".
- *Tri-state rows — require / ignore / exclude* — answers both questions in one control, and is
  the natural extension if the exclude direction is ever wanted. Rejected for now as more
  interaction vocabulary than any other control on the page has.
- *Dim the unselected buckets in place* — keeps the layout stable and the structure visible, but
  opacity is already the hover-focus channel (see 055), and reusing it would make two unrelated
  states look alike.
- *A third dropdown beside Labels and Authors* — consistent, but it would put the status names in
  a second place and leave the swatches, which are the same information, inert two inches away.
