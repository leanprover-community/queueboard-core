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
  - `reviewer_attention.py` / `reviewer_attention_format.py` — per-reviewer queue attention reports and formatting
    helpers. Reports also carry the reviewer's pending acceptance-gate proposals (`proposal_items`, design doc 050),
    rendered as a distinct "Proposed to you" section of the daily DM.
  - `reviewer_load.py` — `build_reviewer_loads(repository)` / `reviewer_load_for(repository, login)`: per-reviewer
    review-load (weighted, matching the assignment engine's capacity gate, **incl. pending assignment proposals**
    per design doc 050) as of the latest cached queue snapshot, plus `format_load_line`. Single authority shared by
    the `assigned-prs` command, the daily reviewer-attention digest, and the reviewer console; read-only (never
    builds a snapshot), returns `{}`/`None` when no snapshot exists.
  - `assignment_suggestions.py` — `suggest_prs_for_reviewer(repository, login, *, labels, limit)`:
    on-demand "what should I review?" (design doc 053). Single authority for which open PRs a
    reviewer could take right now and why not the rest; the Zulip `suggest-prs` command and the
    console suggestions page both render its output and never re-derive eligibility. Shares the
    nightly builder's candidate pool via `reviewer_assignment.prepare_assignment_inputs` (new pool
    exclusions belong there, not at call sites), overrides only the requester's push throttles
    (`away_until`, `auto_assign`, `maximum_capacity`) via a profile substitution — the engine is
    unmodified and correctness rules (authorship, conflicts, opt-outs, cooldowns) stay in force —
    and reads only the trace's `available`/`potential` membership, never the random `picked`, so
    results are deterministic per snapshot. Read-only: never builds a snapshot, persists nothing.
    Refuses a snapshot older than `ANALYZER_ASSIGNMENT_SUGGESTIONS_MAX_SNAPSHOT_AGE_SECONDS`
    (0 disables) so the no-active-rule-set `cache_key="default"` fallback cannot serve a long-dead
    row as live. Label overrides report back both `unknown_labels` (not topic labels here) and
    `dropped_labels` (refused by the `MAX_LABELS` cap) — neither is ever silently discarded.
    Also exports `format_skip_summary` (the shared "why not more?" line) and the STATUS_*/SKIP_*
    constants.
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
  `analyzer.apply_reviewer_assignments` — enable one or the other, not both (enforced:
  the apply task skips itself when the proposals flag is also set). Command:
  `manage.py propose_reviewer_assignments [--repo o/n] [--dry-run] [--enable]`.
- `analyzer.expire_assignment_proposals` — essential-maintenance sweep (design doc 050)
  that expires timed-out proposals and supersedes those whose PR closed/merged, gained a
  human assignee, whose reviewer opted out of the PR, or that left the review queue (per
  the `proposal_validity` predicate and `ANALYZER_ASSIGNMENT_PROPOSAL_ON_QUEUE_EXIT`).
  Performs no GitHub writes and is intentionally **not** gated by the master switch, so
  existing proposals keep draining.
- `analyzer.build_area_stats` / `analyzer.refresh_area_stats`

**Reviewer attention tasks**
- `analyzer.reviewer_attention_daily` — daily sweep that computes reviewer-attention signals
  and delivers the per-reviewer DM. The DM also carries the acceptance-gate "Proposed to you"
  section (design doc 050): pending `AssignmentProposal` rows render distinct from assigned
  PRs, and an un-notified proposal triggers a send even for reviewers with
  `notifications_enabled` off (proposal prompts are transactional; dedupe is
  `AssignmentProposal.notified_at`, stamped after a successful send). There is no separate
  proposal digest task.
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
