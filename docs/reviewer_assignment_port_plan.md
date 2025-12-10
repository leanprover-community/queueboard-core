# Reviewer Assignment Parity and Port Plan

This note captures how the legacy `src/queueboard/suggest_reviewer.py` pipeline works today and lays out a parity-first
plan to move the logic into `qb_site/` (Django + Celery + DRF).

## Legacy algorithm (current behaviour)

- **Inputs**
  - `reviewer-topics.json`: list of reviewer entries with `github_handle`, `zulip_handle`, `top_level` labels, `free_form`,
    optional `maximum_capacity`, `auto_assign`, `temporary_break`, and `conflict_of_interest`.
  - `processed_data/assignment_data.json` (from `process.py`): timestamped map of assignee -> `[{"number", "state"}, ...]`
    plus counts of open/assigned PRs.
  - `api/prs_to_list.json` and `api/aggregate_info.json`: PR partitions and per-PR metadata (`AggregatePRInfo`) produced by
    `dashboard_data.py`. Queue selection uses `Dashboard.QueueStaleUnassigned` and `Dashboard.Queue`.
  - `outdated_prs.txt`: optional blocklist so we do not auto-suggest on PRs just marked outdated in the same run.
- **Reviewer catalog (`read_reviewer_info`)**
  - Loads the JSON file and normalises defaults: `maximum_capacity=10`, `auto_assign=True`, `temporary_break=False`,
    `conflict_of_interest=[]`.
- **Assignment statistics (`collect_assignment_statistics`)**
  - Reads `assignment_data.json` and builds per-reviewer stats for **open** assigned PRs:
    - `assignments[login] -> (open_pr_numbers, open_weighted_load, total_assigned_count)`.
    - Weighted load ignores self-assigned PRs and calls `_compute_weight` per PR using the legacy PR status classifier.
  - `_compute_weight(pr, AggregatePRInfo)`:
    - Builds `PRState` from label kinds (`label_categorisation_rules`), CI rollup, draft flag, and fork status.
    - `determine_PR_status(now=2025-01-01, state)` drives weights:
      - `AwaitingReview|MergeConflict`: weight 1.0.
      - `Blocked|Delegated|AwaitingBors|Closed|Contradictory|NotReady|HelpWanted`: weight 0.
      - `AwaitingAuthor|AwaitingDecision`: if `last_status_change` missing/incomplete -> 0.1, else `1/(delta.days+1)`.
- **Per-PR suggestion (`suggest_reviewers` → `_suggest_reviewers_inner`)**
  - Topic labels considered: any label starting with `t-` or named `CI`, `IMO`, `tech debt`.
  - Candidate pool: reviewers matching at least one topic label (or all reviewers if the PR has no topic labels), excluding
    the author and declared `conflict_of_interest`.
  - If multiple matches: keep reviewers with the highest number of matching topics when `max_score > 1`, otherwise all
    with at least one match. Sort by current weighted load ascending.
  - Availability filter: `maximum_capacity - current_weight > 0` AND `is_on_rotation` AND not `is_temporarily_off_rotation`.
  - Selection: return the sorted candidate list (`all_potential_reviewers`), the subset passing availability (`all_available_reviewers`),
    and a random pick from the available set weighted by remaining capacity. Warnings are logged when nobody is available.
- **Batch suggestion for stale queue (`suggest_reviewers_many`)**
  - Input PRs: shuffled `Dashboard.QueueStaleUnassigned` minus `outdated_prs.txt`.
  - Iterates PRs in ascending order, updating an in-memory copy of `assignments` after each pick so later suggestions see
    the simulated load.
  - Output written to `api/automatic_assignments.json` keyed by PR number.
- **Area stats (`compute_area_ratios`)**
  - Scope: PRs currently on `Dashboard.Queue`.
  - For each area label on those PRs (same topic filter as above), compute:
    - `at_max_capacity`: true when `_suggest_reviewers_inner(..., labels=[area])` finds no available reviewer.
    - Counts: `assigned` vs `unassigned`, `on_queue`, `num_reviewers`, `num_reviewers_on_rotation`.
    - Queue time rollups (seconds): `total_queue_time`, `avg_queue_time`, `assigned_queue_time`, `avg_assigned_queue_time`.
      Missing `total_queue_time` data is treated as 0 and tracked to avoid dividing by zero.
    - Ratio: `on_queue / assigned` (or `None` if nothing is assigned).
    - Styling: hex background/foreground colours derived from label colour; colours are missing when the label is not
      present on any queued PR.
  - Output is `api/area_stats.json`.
- **Data quirks to preserve**
  - Missing/incomplete `last_status_change` falls back to weight 0.1 for author/decision states.
  - Queue time missing → treated as 0 but counts are tracked; average is skipped when all PRs are missing.
  - `assignment_data.json` may report counts inconsistent with its content; code only warns.
  - Random selection means outputs are non-deterministic per run.

## Parity gaps vs. `qb_site/`

- Analyzer snapshot currently sets `last_status_change=None`; weights depend on it for `AwaitingAuthor/Decision`, so we
  need an equivalent source (timeline replay or queue-window-derived deltas).
- Legacy reads `conflict_of_interest` and `zulip_handle`; `ReviewerPreference` does not currently store these fields.
- Legacy queue time uses `TotalQueueTime` seconds; Analyzer emits `total_queue_time.value_td` as seconds and a richer
  `data_status` map. The port should map statuses back to legacy semantics (`missing` → 0 but tracked).
- CI rollup parity differs: Analyzer treats cancelled checks as inessential without name allowlisting. Weighting uses the
  legacy `determine_PR_status`, so CI inputs must match. We now persist GitHub's head commit rollup
  (`head_ci_state`) to detect untracked failures as inessential without storing every job.
- Legacy filters queue PRs by default-branch labels/CI + dashboard logic; Analyzer queue lists come from
  `QueueboardSnapshotBuilder` + `QueueRuleSet`. We should keep using the same dashboard partition for picking PRs.

## Port plan for `qb_site/`

- **Goal**: serve `automatic_assignments` and `area_stats` (plus the underlying reviewer suggestion service) from Django
  with behaviour matching `src/queueboard/suggest_reviewer.py` while reusing synced DB state.

- **Data loading**
  - Reviewer preferences: use `ReviewerPreference` rows scoped to the target repo/rule set. Extend the model or add a
    companion table/JSON field for `conflict_of_interest` (and optional Zulip handle) to avoid dropping behaviour.
  - PR metadata: read open `PullRequest` rows with labels (`PRLabel`), CI rollups, assignees, approvals, commenters,
    queue windows (`PRQueueWindow`), and timeline completeness flags.
  - PR status and queue lists: reuse `QueueboardSnapshotBuilder` outputs or an equivalent builder to avoid duplicating
    dashboard partitioning logic (Queue, QueueStale*, etc.).
  - Last status change: port `state_evolution.py` or derive from queue windows to produce `last_status_change` deltas
    usable by the weight function (ideally the former for true parity).

- **Services (Analyzer)**
  1) `ReviewerCatalogService`
     - Hydrates reviewer entries from `ReviewerPreference` (+ conflict/zulip, away_until -> temporary_break surrogate).
     - Applies defaults: capacity=10, auto_assign default True, temporary_break based on `auto_assign=False` or
       `away_until > now`.
  2) `AssignmentStatsService`
     - Scans open PRs with assignees to build `AssignmentStatistics` (open list, per-user weighted load, counts of
       multiple assignees). Weighting uses the legacy `_compute_weight` port with PRStatus classification and
       `last_status_change` data.
  3) `ReviewerSuggestionService`
     - Implements `_suggest_reviewers_inner` parity (topic matching, conflict/author exclusion, max-score filtering,
       load-based sort, capacity/rotation filter, weighted random pick). Provide deterministic mode for tests.
  4) `BatchSuggestionService`
     - Accepts a list of target PR numbers (default: queued stale unassigned) and returns the PR→reviewer map while
       simulating load updates between picks.
  5) `AreaStatsService`
     - Uses queue PRs + queue time fields to compute area metrics and capacity flag; mirrors `compute_area_ratios`.

- **Persistence and caching**
  - Extend `QueueSnapshot` payload to include `automatic_assignments` and `area_stats`, or add a sibling
    `ReviewerAssignmentSnapshot` keyed by `(repo, rule_set, generated_at)` with ETag/Last-Modified for DRF views.
  - Builder should accept `generated_at` to keep snapshot and assignments aligned; reuse cached snapshots when still fresh.

- **Tasks**
  - Celery task (e.g., `analyzer.tasks.build_reviewer_assignments`) that:
    1) Loads or builds the latest queue snapshot for a repo/rule set.
    2) Computes assignment stats, batch suggestions over the snapshot’s `QueueStaleUnassigned` list (respecting
       `outdated_prs` equivalent if we keep it), and area stats over `Queue`.
    3) Stores the payload (and metadata such as counts, generated_at, etag) for API serving.
  - Hook into existing beat schedules or snapshot build pipeline so assignments refresh alongside snapshots.
  - Admin/CLI hooks to force a rebuild for a repo/rule set.
  - CI rollup parity: persist `head_ci_state` on `PullRequest` from the bundle’s `statusCheckRollup.state` and
    treat “head is red but tracked checks passed” as inessential failure in snapshot rollup; backfill by
    re-syncing open PRs and include missing `head_ci_state` in engagement backfill/convergence metrics.

- **API surface (DRF)**
  - `GET /api/v1/queueboard/automatic_assignments?repo=owner/name[&rule_set_id=...]`
    - Returns `{ "<pr_number>": "<github_login>", "meta": { "generated_at": "...", "rule_set_id": ... } }`.
  - `GET /api/v1/queueboard/area_stats?repo=owner/name[&rule_set_id=...]`
    - Returns the legacy `area_stats.json` shape plus `meta` and `data_status` for queue time coverage.
  - Optionally embed both blobs inside `queueboard/snapshot` responses (or add `/bundle` endpoint) so the CLI adapter can
    fetch everything in one request.
  - Support ETag/Last-Modified and a 202/refresh pattern mirroring the dependency-graph view.

- **CLI/renderer bridge**
  - Add an `--source api` path in `dashboard_data.py` (or a new Django management command) to fetch the API payloads and
    emit the same `api/*.json` files locally, keeping the HTML renderer unchanged during rollout.

- **Testing**
  - Unit tests for weight calculation and availability filtering against fixture PRs (author/conflict, capacity edge
    cases, missing `last_status_change`).
  - Snapshot tests comparing `automatic_assignments`/`area_stats` outputs to legacy fixtures derived from `test/`.
  - Integration tests for DRF views (200/202/304 and rule set selection) and Celery task wiring.

- **Open follow-ups**
  - Decide how to represent and ingest `conflict_of_interest` (new JSONField vs. join table) and whether to expose
    Zulip handles in the API.
  - Align CI rollup with legacy `determine_ci_status` name allowlist or document intended divergence.
  - If timeline replay is postponed, define the queue-window-derived surrogate for `last_status_change` used in weighting
    and mark any data_status differences in responses.
