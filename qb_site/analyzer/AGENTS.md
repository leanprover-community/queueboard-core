# Analyzer Guidelines

## Scope
- `qb_site/analyzer/` owns derived analytics/state built from syncer raw data:
  - PR revisions (`PRRevision`, `PRRevisionBuildState`),
  - queue windows/rulesets,
  - dependency extraction/state,
  - snapshots (queueboard/reviewer assignment/area stats/convergence).
- Keep derived logic in `services/` and orchestration/sweeps in `tasks/`.
- Key read-only services:
  - `queueboard_snapshot.py` — builds and caches the full per-repo queue snapshot payload.
  - `reviewer_attention.py` / `reviewer_attention_format.py` — per-reviewer queue attention reports and formatting helpers.
  - `pr_info.py` — `get_pr_queue_info(owner, repo, pr_number)`: returns `PRQueueInfo` for a single PR; prefers the default `QueueSnapshot`, falls back to direct DB queries for merged/closed PRs.

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

# Backfill queue-window build states (e.g. after adding a new build-state field)
docker compose exec -T web python qb_site/manage.py backfill_queue_window_build_states --dry-run
```

## Task Surface
Celery task names (as registered via `@shared_task(name=…)`):

**Per-PR processing**
- `analyzer.process_pr` — orchestrates revisions, queue windows, dependency parsing, and CI-by-SHA planning for a single PR after syncer ingest.

**Sweep / periodic tasks**
- `analyzer.plan_missing_ci` — identifies revision heads with no CI data and enqueues CI-by-SHA syncs.
- `analyzer.rebuild_revisions_sweep` — sweeps all repos to rebuild PR revision windows.
- `analyzer.rebuild_queue_windows_sweep` — sweeps all repos to rebuild queue-window rows.
- `analyzer.collect_convergence` — records convergence analytics snapshots.

**Dependency tasks**
- `analyzer.rebuild_pr_dependencies` — rebuilds dependency edges for a single PR.
- `analyzer.rebuild_dependencies_sweep` — sweeps all repos to rebuild PR dependency state.

**Snapshot / assignment tasks**
- `analyzer.build_queueboard_snapshot` / `analyzer.refresh_queueboard_snapshots`
- `analyzer.build_reviewer_assignment` / `analyzer.refresh_reviewer_assignments`
- `analyzer.build_area_stats` / `analyzer.refresh_area_stats`

**Reviewer attention tasks**
- `analyzer.reviewer_attention_daily` — daily sweep that computes reviewer-attention signals.
- `analyzer.reviewer_attention_cleanup` — prunes stale reviewer-attention records.

Keep tasks idempotent and resumable; prefer explicit summary payloads to aid admin/task-result debugging.

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
