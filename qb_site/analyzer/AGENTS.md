# Analyzer Guidelines

## Scope
- `qb_site/analyzer/` owns derived analytics/state built from syncer raw data:
  - PR revisions (`PRRevision`, `PRRevisionBuildState`),
  - queue windows/rulesets,
  - dependency extraction/state,
  - snapshots (queueboard/reviewer assignment/area stats/convergence).
- Keep derived logic in `services/` and orchestration/sweeps in `tasks/`.

## High-Value Commands
```bash
# App test suite
docker compose exec -T web env DJANGO_SETTINGS_MODULE=qb_site.settings.ci python qb_site/manage.py test analyzer

# Rebuild revisions command
docker compose exec -T web python qb_site/manage.py rebuild_revisions --repo leanprover-community/mathlib4 --number 30723

# Plan CI backfill command
docker compose exec -T web python qb_site/manage.py plan_ci_backfill --repo leanprover-community/mathlib4 --enqueue

# Backfill reviewer opt-outs command
docker compose exec -T web python qb_site/manage.py backfill_reviewer_opt_outs --dry-run
```

## Task Surface
- Periodic analyzer tasks include:
  - `analyzer.plan_missing_ci`,
  - `analyzer.rebuild_revisions_sweep`,
  - `analyzer.rebuild_queue_windows_sweep`,
  - `analyzer.rebuild_dependencies_sweep`,
  - snapshot refresh tasks (`queueboard`, reviewer assignment, area stats),
  - `analyzer.collect_convergence`.
- Keep tasks idempotent and resumable; prefer explicit summary payloads to aid admin/task-result debugging.

## Testing Expectations
- Canonical full validation still goes through `bash scripts/repo_check_compose.sh`.
- That script may be unavailable in sandboxed environments because it starts Docker/DB services.
- If Compose cannot run:
  - run targeted analyzer tests and pure service tests,
  - report any DB/scheduler coverage gaps clearly.

## Design and Data Notes
- Preserve boundary:
  - syncer stores raw GitHub facts,
  - analyzer stores derived interpretations/ruleset-dependent materializations.
- When changing queue-window/revision semantics, update corresponding design docs in `docs/design-decisions/`.
- Favor builder-versioned or state-tracked sweeps for large backfills to avoid full-table churn.

## Operational Notes
- Large sweeps can contend with sync tasks on shared worker capacity; tune per-repo limits before broadening cadence.
- Keep admin/object-tool commands available for targeted per-PR recovery paths.
