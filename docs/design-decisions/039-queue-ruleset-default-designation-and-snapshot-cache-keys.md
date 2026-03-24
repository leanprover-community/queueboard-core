# Queue Ruleset Default Designation and Snapshot Cache Keys

## Context

`QueueRuleSet` rows are per-repository and versioned. Multiple active rulesets
can coexist (e.g. with `effective_from`/`effective_to` windows for historical
re-evaluation). The `QueueSnapshot`, `ReviewerAssignmentSnapshot`, and
`AreaStatsSnapshot` models use a `cache_key` field to store multiple snapshot
variants per repository; the periodic refresh tasks key each snapshot by
`str(rule_set.id)`.

Before this change, all code that needed "the canonical snapshot for a repo"
used the implicit convention of picking the highest-version active ruleset and
falling back to `cache_key="default"` when no rulesets existed. Several
callsites hardcoded `cache_key="default"` directly:

- Admin build buttons (`core/admin.py`, `analyzer/admin.py`) always wrote to
  `"default"`, regardless of which rulesets were configured.
- The dependency graph API view (`api/views/queueboard_dependency_graph.py`)
  defaulted `cache_key` from the query parameter with a fallback of `"default"`,
  never resolving the active ruleset. On every stale or missing snapshot request
  it enqueued `build_queueboard_snapshot` without a `rule_set_id`, creating a
  perpetually-refreshed `cache_key="default"` snapshot out of step with the
  beat-schedule-driven ruleset-keyed ones.

## Decision

Add an explicit `is_default = BooleanField(default=False)` to `QueueRuleSet`.
A partial unique constraint (`analyzer_queueruleset_repo_single_default`)
enforces at most one default per repository at the database level.

Introduce `default_rule_set_for_repo(repo) -> QueueRuleSet | None` in
`analyzer/services/queue_rules.py` as the single authoritative lookup:

1. Return the active ruleset with `is_default=True` if one exists.
2. Otherwise fall back to the highest-version active ruleset (preserves prior
   implicit behaviour for repos that haven't designated a default yet).
3. Return `None` if no active rulesets exist (e.g. a newly added repository).

All consumers are updated to call this helper rather than repeating the
"highest version" query inline:

- `load_rules_for_repo` (no-`at` path)
- `QueueboardSnapshotBuilder._default_rule_set`
- `analyzer/services/pr_info.py`
- `api/views/queueboard_snapshot.py` (no-param path)
- `api/views/queueboard_dependency_graph.py` (fixes the spurious `"default"`
  snapshot generation)
- All admin build buttons in `core/admin.py` and `analyzer/admin.py`

## Failure modes and graceful degradation

- **No `is_default` set, active rulesets exist:** falls back to highest version.
  Behaviour is identical to pre-change; the flag is opt-in.
- **`is_default=True` ruleset is deactivated without designating a new default:**
  the inactive row is excluded by the `is_active=True` filter and the system
  falls back to highest-version active. Silent but safe.
- **No active rulesets at all** (new repo): `default_rule_set_for_repo` returns
  `None`; all consumers use `cache_key="default"` and `load_rules_for_repo`
  returns minimal `QueueRules()` (open + not-draft only). The periodic refresh
  task writes to the `"default"` slot in this case.

## Periodic tasks

The beat-schedule refresh tasks (`analyzer.refresh_queueboard_snapshots` etc.)
are unchanged — they already iterate active rulesets and key each snapshot by
`str(rule_set.id)`. The `cache_key="default"` parameter they receive from the
beat schedule is only used as a fallback for repos with no active rulesets.

## Setting the default in production

In the Django admin, navigate to Analyzer → Queue Rule Sets, select the
canonical ruleset for a repository, and tick `is_default`. The partial unique
constraint prevents more than one per repository. No task restart or snapshot
rebuild is required; all consumers pick up the new designation on their next
request.
