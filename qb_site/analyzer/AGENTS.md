# Analyzer Guidelines

## Scope
- `qb_site/analyzer/` owns derived analytics/state built from syncer raw data:
  - PR revisions (`PRRevision`, `PRRevisionBuildState`),
  - queue windows/rulesets,
  - dependency extraction/state,
  - snapshots (queueboard/reviewer assignment/area stats/convergence).
- Keep derived logic in `services/` and orchestration/sweeps in `tasks/`.
- Key read-only services:
  - `queueboard_snapshot.py` — builds and caches the full per-repo queue snapshot payload. Each PR
    entry carries a `proposal` field (`{reviewer, expires_at}` or `null`) for the acceptance-gate
    "proposed to X" state (design doc 050), surfaced distinct from `assignees`.
  - `reviewer_attention.py` / `reviewer_attention_format.py` — per-reviewer queue attention reports and formatting helpers.
  - `reviewer_load.py` — `build_reviewer_loads(repository)` / `reviewer_load_for(repository, login)`: per-reviewer
    review-load (weighted, matching the assignment engine's capacity gate) as of the latest cached queue snapshot,
    plus `format_load_line`. Single authority shared by the `assigned-prs` command and the daily reviewer-attention
    digest; read-only (never builds a snapshot), returns `{}`/`None` when no snapshot exists.
  - `pr_info.py` — `get_pr_queue_info(owner, repo, pr_number)`: returns `PRQueueInfo` for a single PR; prefers the default `QueueSnapshot`, falls back to direct DB queries for merged/closed PRs. Also exposes the acceptance-gate `proposed_to`/`proposal_expires_at` (design doc 050), read live from the single active `AssignmentProposal`, distinct from `assignee_logins`.
  - `ci_evaluation.py` — single-PR CI status evaluation against a ruleset's `required_ci_contexts`; use `ci_status_for_pr(pr, rules, repository)` instead of re-implementing context-matching logic.

## High-Value Commands
```bash
# App test suite
docker compose exec -T web env DJANGO_SETTINGS_MODULE=qb_site.settings.ci python qb_site/manage.py test analyzer

# Rebuild revisions command
docker compose exec -T web python qb_site/manage.py rebuild_revisions --repo leanprover-community/mathlib4 --pr 30723

# Plan CI backfill command
docker compose exec -T web python qb_site/manage.py plan_ci_backfill --repo leanprover-community/mathlib4 --enqueue

# Backfill reviewer opt-outs command
docker compose exec -T web python qb_site/manage.py backfill_reviewer_opt_outs --dry-run

# Audit PRRevision window contiguity (read-only; design decision 049)
docker compose exec -T web python qb_site/manage.py audit_revision_contiguity --repo leanprover-community/mathlib4
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
- `analyzer.apply_reviewer_assignments` — applies the latest default-rule-set
  assignment snapshot to GitHub (POSTs assignees via the `assign_pr` operation),
  recording outcomes in `ReviewerAssignmentApplication`. Gated by
  `ANALYZER_REVIEWER_ASSIGNMENT_APPLY_ENABLED` (+ dry-run). Replaces the legacy
  GitHub Actions auto-assign workflow; see design doc 046.
- `analyzer.propose_reviewer_assignments` — acceptance-gate variant of the apply step
  (design doc 050). Per snapshot `{pr: login}`, branches on the reviewer's
  `ReviewerPreference.assignment_acceptance`: `auto` (and `confirm` reviewers with no
  Zulip link) are direct-assigned via the shared 046 mutation path; `confirm` reviewers
  with a Zulip link get an `AssignmentProposal` awaiting console acceptance. Gated by
  `ANALYZER_ASSIGNMENT_PROPOSALS_ENABLED` (+ dry-run). **Supersedes**
  `analyzer.apply_reviewer_assignments` — enable one or the other, not both. Command:
  `manage.py propose_reviewer_assignments [--repo o/n] [--dry-run] [--enable]`.
- `analyzer.expire_assignment_proposals` — essential-maintenance sweep (design doc 050)
  that expires timed-out proposals and supersedes those whose PR closed/merged, gained a
  human assignee, or left the review queue (per the `proposal_validity` predicate and
  `ANALYZER_ASSIGNMENT_PROPOSAL_ON_QUEUE_EXIT`). Performs no GitHub writes and is
  intentionally **not** gated by the master switch, so existing proposals keep draining.
- `analyzer.deliver_assignment_proposals` — sends one per-reviewer Zulip DM digest
  (design doc 050) of their pending, not-yet-notified proposals across all repos, linking
  to the console. Dedupe is carried by `AssignmentProposal.notified_at` (stamped after a
  successful send; no separate record model). Requires BOTH
  `ANALYZER_ASSIGNMENT_PROPOSALS_ENABLED` and `ANALYZER_ASSIGNMENT_PROPOSALS_DELIVERY_ENABLED`
  to actually send (+ `_DRY_RUN` computes the would-send set). Reachability is
  `core.User.zulip_user_id`. Command:
  `manage.py deliver_assignment_proposals [--repo o/n] [--dry-run] [--enable]`.
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
- Sweeps and `analyzer.process_pr` rebuild the same derived rows concurrently (sweeps
  preferentially pick freshly-updated PRs — the same ones process_pr is handling).
  Follow "Concurrent Writers and Unique Keys" in `qb_site/AGENTS.md`: upsert instead of
  check-then-create, wrap rebuilds in `transaction.atomic()`, and contain per-PR
  `IntegrityError` in sweeps so one conflict cannot abort the run.
