# Queue Windows First, Defer Other Intervals

## Context
- Analyzer needs to answer questions about time on the queue and review cycles per PR (e.g., “how many queue cycles before merge?”).
- We have rich raw inputs already stored in Syncer:
  - `syncer.PRTimelineEvent` for labels, draft/ready, reopen/close, force‑push.
  - `syncer.CheckRun` and `syncer.StatusContext` for CI snapshots.
- The Analyzer plan mentions several possible interval tables:
  - Label state windows, CI state windows, head revision windows, and queue windows.
- Interval tables are attractive because they can speed up repeated queries and multiple rule versions, but they also introduce extra schema, recompute logic, and migration overhead.
- We are actively evolving `QueueRuleSet` and want to ship a first version of queue membership + time on queue without over‑designing the intermediate storage.

## Decision
- Implement **queue windows first** and defer additional rule‑independent windows (label/CI/open intervals) until we have concrete performance or reuse pressure.
- Queue windows:
  - Are computed in Analyzer from raw Syncer data (timeline + CI) and a given `QueueRuleSet`.
  - Will be persisted as Analyzer models and versioned by `QueueRuleSet` (ruleset FK or version field) so that:
    - Different PRs can be evaluated under different rulesets.
    - New ruleset versions do not mutate existing windows; they add new rows.
- Label and CI intervals:
  - Will **not** be modeled as separate tables for now.
  - Will remain implicit in the queue window builder, which replays `PRTimelineEvent` and CI snapshots directly.

## Consequences
- Pros
  - Faster path to a useful feature: queue membership + time on queue + review cycle counts, without extra schema.
  - Less coordination and migration overhead while `QueueRuleSet` is still stabilizing.
  - Avoids premature design of label/CI interval models before we know which shapes are most useful (per‑context vs per‑PR, daily vs arbitrary windows).
  - Queue windows can still be used as the basis for `QueueDailySnapshot` / `PRQueueDailySpan` materializations.
- Cons
  - Queue window computation replays raw timelines and CI snapshots each time; for very active PRs this may be more work than replaying shorter interval sequences.
  - Multiple rulesets over the same PR will each compute their own windows from raw events; we do not share precomputed label/CI intervals across rulesets.
  - Later introduction of label/CI interval tables will require migration and backfill code to derive them from existing history.

## Operational Notes
- Queue windows
  - Implement a queue window builder in Analyzer that:
    - Reads `PullRequest` + `PRTimelineEvent` + CI snapshots from Syncer.
    - Applies a `QueueRuleSet` (labels, CI gating, draft/open rules) to produce `[enter, exit)` queue windows.
    - Treat each queue window as a single “queue cycle” for now; `cycle_index` is the window index per `(pr, rule_set)`.
  - Store queue windows in an Analyzer model keyed by `(pr, rule_set, from_ts, to_ts)` with a `rules_version` column or a FK to `QueueRuleSet`.
  - Treat `QueueRuleSet` as append‑only; do not mutate existing rows. New rules mean new rows, not in‑place edits.
  - Derive daily aggregates (`QueueDailySnapshot`, `PRQueueDailySpan`) from queue windows for selected rulesets as needed.
- Future label/CI intervals
  - If queries become CI‑heavy or we need fine‑grained CI‑at‑time answers across many analyses, introduce CI interval models (e.g., `CommitCIRollup` / `PRCIStateInterval`) first.
  - If replaying label events becomes a bottleneck, introduce a `PRLabelInterval` model as a rule‑independent helper and update the queue window builder to consume it.
  - Any new interval table should have a “rebuild for PR” service that recomputes from raw Syncer data and is safe to run idempotently after backfills.

## Alternatives (Optional)
- **Build label/CI intervals now**
  - Pros: queue windows and future analyses would reuse shared interval tables; less recomputation per ruleset.
  - Cons: more upfront schema and complexity before we know the exact query patterns; higher implementation cost for the initial Analyzer rollout.
- **Never store queue windows; compute on read only**
  - Pros: simplest storage; no queue‑specific tables.
  - Cons: makes “review cycle” analysis and historical backfills more expensive; harder to version by ruleset and to support daily snapshots efficiently.
