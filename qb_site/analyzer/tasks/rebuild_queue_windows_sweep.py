from __future__ import annotations

from celery import shared_task
from datetime import datetime
from django.utils import timezone

from django.db.models import Count, Exists, F, Min, OuterRef, Q

from analyzer.models import PRQueueWindow, PRQueueWindowBuildState, PRRevision, PRRevisionBuildState, QueueRuleSet
from analyzer.models.queue_window import QueueWindowEventType
from analyzer.services.queue_window_build_state import record_queue_window_build_states
from analyzer.services.queue_windows import rebuild_queue_windows_for_pr
from core.models import Repository
from syncer.models import PullRequest


_CI_ATTRIBUTION_TYPES = [QueueWindowEventType.CI_PASSED, QueueWindowEventType.CI_FAILED]


def _is_ruleset_stale_for_pr(
    *,
    rule_set: QueueRuleSet,
    state: PRRevisionBuildState,
    rs_state: PRQueueWindowBuildState | None,
    has_rollup_backfill: bool,
    has_attribution_backfill: bool = False,
    pr_gh_updated_at: datetime | None = None,
) -> bool:
    """Return whether queue windows are stale for a specific (PR, ruleset) pair.

    This is the *exact* per-ruleset staleness check executed in Python after the
    SQL prefilter (``needs_rebuild``) narrows the candidate PR set.  Every condition
    here must have a corresponding conservative signal in the outer SQL filter so that
    the prefilter does not produce false negatives (i.e., it may include extra PRs but
    must never miss a stale one).

    NOTE: keep these staleness conditions in sync with the ``stale`` calculation
    in ``collect_analyzer_convergence_task`` (collect_convergence.py), which uses
    the same logic to count stale ``(PR, ruleset)`` pairs for the convergence metric.

    Staleness sources and their SQL counterparts
    -------------------------------------------
    - No build-state row              → ``active_ruleset_state_count < len(rule_set_ids)``
    - ``revision_version_built`` null → ``null_ruleset_state_revision_count > 0``
    - ``revision_version_built`` lag  → ``min_ruleset_state_revision_built < revision_version``
    - ``windows_built_at`` null       → ``null_ruleset_state_windows_built_at_count > 0``
    - Ruleset ``updated_at`` changed  → ``min_ruleset_state_windows_built_at < max_ruleset_updated_at``
    - Label/state change (``gh_updated_at``) → ``min_ruleset_state_windows_built_at < gh_updated_at``
    - Rollup fields missing           → ``has_rollup_backfill=True`` (Exists subquery)
    - Attribution fields missing/inconsistent → ``has_attribution_backfill=True`` (Exists subquery)
      Covers: pre-migration windows (opened_by_event_type IS NULL) and post-expire-task
      partial failures (CI event_type with both CI FKs null).
    """
    # Existing windows have missing rollup fields (window_count=0 or first_on_queue_ts=None).
    if has_rollup_backfill:
        return True

    # Existing windows have missing or inconsistent attribution fields.
    if has_attribution_backfill:
        return True

    # No build-state row yet for this (PR, ruleset) pair.
    if rs_state is None:
        return True

    # Build-state freshness fields are null — windows were never successfully recorded.
    if rs_state.revision_version_built is None:
        return True
    # A new revision was built after the last queue-window rebuild.
    if rs_state.revision_version_built < state.revision_version:
        return True
    if rs_state.windows_built_at is None:
        return True
    # The ruleset definition changed (e.g. forbidden_label_names updated) after last rebuild.
    if rule_set.updated_at and rs_state.windows_built_at < rule_set.updated_at:
        return True
    # GitHub bumps pr.gh_updated_at on label events, review-state changes, and other
    # PR metadata changes.  If windows were built before that timestamp, queue membership
    # may have changed (e.g. a forbidden label was added/removed) without a revision bump.
    if pr_gh_updated_at and rs_state.windows_built_at < pr_gh_updated_at:
        return True
    return False


@shared_task(name="analyzer.rebuild_queue_windows_sweep")
def rebuild_queue_windows_sweep_task(
    *,
    max_prs_per_repo: int = 50,
    only_complete_backfill: bool = False,
) -> dict:
    """Rebuild queue windows for PRs whose revision_version changed or windows are stale."""
    max_pr_list = 10
    now_ts = timezone.now()
    repos = list(Repository.objects.filter(is_active=True).only("id", "owner", "name"))
    total_rebuilt = 0
    total_prs = 0
    total_prs_skipped_up_to_date = 0
    total_rulesets_skipped_out_of_bounds = 0
    total_prs_stale_ruleset = 0
    total_prs_rebuilt_stale_ruleset = 0
    processed_pr_numbers: list[int] = []
    per_repo: list[dict] = []

    for repo in repos:
        rulesets = list(QueueRuleSet.objects.filter(repository=repo, is_active=True))
        max_ruleset_updated_at = max((rs.updated_at for rs in rulesets if rs.updated_at is not None), default=None)
        rule_set_ids = [int(rs.id) for rs in rulesets]

        has_revisions = PRRevision.objects.filter(pull_request=OuterRef("pk"))
        rollup_backfill = PRQueueWindow.objects.filter(
            pull_request=OuterRef("pk"),
            rule_set_id__in=rule_set_ids,
        ).filter(Q(window_count=0) | Q(first_on_queue_ts__isnull=True))
        attribution_backfill = PRQueueWindow.objects.filter(
            pull_request=OuterRef("pk"),
            rule_set_id__in=rule_set_ids,
        ).filter(
            # Pre-migration: event_type not yet populated.
            Q(opened_by_event_type__isnull=True)
            # Post-expire-task partial failure: CI event type but both CI FKs null.
            | Q(
                opened_by_event_type__in=_CI_ATTRIBUTION_TYPES,
                opened_by_check_run__isnull=True,
                opened_by_status_context__isnull=True,
            )
            | Q(
                closed_by_event_type__in=_CI_ATTRIBUTION_TYPES,
                closed_by_check_run__isnull=True,
                closed_by_status_context__isnull=True,
            )
        )

        pr_qs = PullRequest.objects.filter(repository=repo, timeline_backfill_done=True)
        if only_complete_backfill:
            pr_qs = pr_qs.filter(commits_backfill_done=True)
        pr_qs = pr_qs.select_related("revision_build_state").only(
            "id",
            "number",
            "gh_created_at",
            "gh_updated_at",
            "timeline_backfill_done",
            "commits_backfill_done",
            "revision_build_state__revision_version",
        )
        pr_qs = pr_qs.annotate(
            has_revisions=Exists(has_revisions),
            has_rollup_backfill=Exists(rollup_backfill),
            has_attribution_backfill=Exists(attribution_backfill),
        ).filter(has_revisions=True)
        pr_qs = pr_qs.annotate(
            active_ruleset_state_count=Count(
                "queue_window_build_states",
                filter=Q(queue_window_build_states__rule_set_id__in=rule_set_ids),
                distinct=True,
            ),
            null_ruleset_state_revision_count=Count(
                "queue_window_build_states",
                filter=Q(queue_window_build_states__rule_set_id__in=rule_set_ids)
                & Q(queue_window_build_states__revision_version_built__isnull=True),
                distinct=True,
            ),
            null_ruleset_state_windows_built_at_count=Count(
                "queue_window_build_states",
                filter=Q(queue_window_build_states__rule_set_id__in=rule_set_ids)
                & Q(queue_window_build_states__windows_built_at__isnull=True),
                distinct=True,
            ),
            min_ruleset_state_revision_built=Min(
                "queue_window_build_states__revision_version_built",
                filter=Q(queue_window_build_states__rule_set_id__in=rule_set_ids),
            ),
            min_ruleset_state_windows_built_at=Min(
                "queue_window_build_states__windows_built_at",
                filter=Q(queue_window_build_states__rule_set_id__in=rule_set_ids),
            ),
        )

        # Conservative SQL prefilter: select PRs that *might* have stale queue windows.
        # This may include false positives (handled by the exact per-PR check below), but
        # must never produce false negatives.  Every condition in _is_ruleset_stale_for_pr
        # has a corresponding approximate signal here.
        needs_rebuild = (
            # PR has no revision build-state at all — treat as stale.
            Q(revision_build_state__isnull=True)
            # At least one existing window row is missing rollup fields.
            | Q(has_rollup_backfill=True)
            # At least one existing window row has missing/inconsistent attribution fields.
            | Q(has_attribution_backfill=True)
            # Fewer build-state rows than active rulesets — at least one ruleset is missing.
            | Q(active_ruleset_state_count__lt=len(rule_set_ids))
            # At least one build-state row has a null revision_version_built.
            | Q(null_ruleset_state_revision_count__gt=0)
            # The oldest built revision is behind the current revision_version.
            | Q(min_ruleset_state_revision_built__lt=F("revision_build_state__revision_version"))
            # At least one build-state row has a null windows_built_at.
            | Q(null_ruleset_state_windows_built_at_count__gt=0)
        )
        # The oldest windows_built_at predates the most recently updated ruleset definition.
        if max_ruleset_updated_at is not None:
            needs_rebuild |= Q(min_ruleset_state_windows_built_at__lt=max_ruleset_updated_at)
        # Detect staleness from label/state changes: GitHub bumps updated_at on label events,
        # and the syncer stores this as gh_updated_at. If windows were built before that
        # timestamp, queue membership may have changed without a revision bump.
        # Using an F() expression avoids a correlated subquery and keeps this O(1) per row.
        needs_rebuild |= Q(min_ruleset_state_windows_built_at__lt=F("gh_updated_at"))
        pr_qs = pr_qs.filter(needs_rebuild).order_by("-gh_updated_at", "-id").iterator(chunk_size=200)

        repo_rebuilt = 0
        repo_prs = 0
        repo_prs_skipped_up_to_date: list[int] = []
        repo_rulesets_skipped_out_of_bounds: list[int] = []
        repo_prs_stale_ruleset: list[int] = []
        repo_prs_rebuilt_stale_ruleset: list[int] = []
        repo_prs_skipped_up_to_date_seen: set[int] = set()
        repo_rulesets_skipped_out_of_bounds_seen: set[int] = set()
        repo_prs_stale_ruleset_seen: set[int] = set()
        repo_prs_rebuilt_stale_ruleset_seen: set[int] = set()
        repo_limit_hit = False

        if not rulesets:
            per_repo.append(
                {
                    "repo": f"{repo.owner}/{repo.name}",
                    "prs_checked": 0,
                    "windows_rebuilt": 0,
                    "prs_skipped_up_to_date": 0,
                    "rulesets_skipped_out_of_bounds": 0,
                    "prs_stale_ruleset": 0,
                    "prs_rebuilt_stale_ruleset": 0,
                    "limit_hit": False,
                }
            )
            continue

        def _process_batch(pr_batch: list[PullRequest]) -> None:
            nonlocal repo_limit_hit, repo_rebuilt, repo_prs, total_prs
            if not pr_batch:
                return

            pr_ids = [int(pr.id) for pr in pr_batch]

            missing_state_pr_ids: list[int] = []
            states_by_pr_id: dict[int, PRRevisionBuildState] = {}
            for pr in pr_batch:
                try:
                    states_by_pr_id[int(pr.id)] = pr.revision_build_state
                except PRRevisionBuildState.DoesNotExist:
                    missing_state_pr_ids.append(int(pr.id))

            if missing_state_pr_ids:
                PRRevisionBuildState.objects.bulk_create(
                    [PRRevisionBuildState(pull_request_id=pr_id) for pr_id in missing_state_pr_ids],
                    ignore_conflicts=True,
                    batch_size=200,
                )
                for row in PRRevisionBuildState.objects.filter(pull_request_id__in=missing_state_pr_ids):
                    states_by_pr_id[int(row.pull_request_id)] = row

            rs_states_by_pr_id: dict[int, dict[int, PRQueueWindowBuildState]] = {}
            for row in PRQueueWindowBuildState.objects.filter(
                pull_request_id__in=pr_ids,
                rule_set_id__in=rule_set_ids,
            ):
                rs_states_by_pr_id.setdefault(int(row.pull_request_id), {})[int(row.rule_set_id)] = row

            rollup_backfill_pairs = set(
                PRQueueWindow.objects.filter(
                    pull_request_id__in=pr_ids,
                    rule_set_id__in=rule_set_ids,
                )
                .filter(Q(window_count=0) | Q(first_on_queue_ts__isnull=True))
                .values_list("pull_request_id", "rule_set_id")
            )
            attribution_backfill_pairs = set(
                PRQueueWindow.objects.filter(
                    pull_request_id__in=pr_ids,
                    rule_set_id__in=rule_set_ids,
                )
                .filter(
                    Q(opened_by_event_type__isnull=True)
                    | Q(
                        opened_by_event_type__in=_CI_ATTRIBUTION_TYPES,
                        opened_by_check_run__isnull=True,
                        opened_by_status_context__isnull=True,
                    )
                    | Q(
                        closed_by_event_type__in=_CI_ATTRIBUTION_TYPES,
                        closed_by_check_run__isnull=True,
                        closed_by_status_context__isnull=True,
                    )
                )
                .values_list("pull_request_id", "rule_set_id")
            )

            for pr in pr_batch:
                if repo_prs >= int(max_prs_per_repo):
                    repo_limit_hit = True
                    return

                pr_id = int(pr.id)
                state = states_by_pr_id.get(pr_id)
                if state is None:
                    continue
                existing_rs_states = rs_states_by_pr_id.get(pr_id, {})
                stale_rule_sets = [
                    rs
                    for rs in rulesets
                    if _is_ruleset_stale_for_pr(
                        rule_set=rs,
                        state=state,
                        rs_state=existing_rs_states.get(int(rs.id)),
                        has_rollup_backfill=(pr_id, int(rs.id)) in rollup_backfill_pairs,
                        has_attribution_backfill=(pr_id, int(rs.id)) in attribution_backfill_pairs,
                        pr_gh_updated_at=pr.gh_updated_at,
                    )
                ]
                stale_ruleset = bool(stale_rule_sets)
                if stale_ruleset:
                    pr_num = int(pr.number)
                    if pr_num not in repo_prs_stale_ruleset_seen:
                        repo_prs_stale_ruleset_seen.add(pr_num)
                    if len(repo_prs_stale_ruleset) < max_pr_list:
                        repo_prs_stale_ruleset.append(pr_num)
                if not stale_ruleset:
                    pr_num = int(pr.number)
                    if pr_num not in repo_prs_skipped_up_to_date_seen:
                        repo_prs_skipped_up_to_date_seen.add(pr_num)
                        if len(repo_prs_skipped_up_to_date) < max_pr_list:
                            repo_prs_skipped_up_to_date.append(pr_num)
                    continue

                summary = rebuild_queue_windows_for_pr(pr=pr, rule_sets=stale_rule_sets)
                per_ruleset = summary.get("per_ruleset", {}) or {}
                pr_num = int(pr.number)
                if any(
                    res.get("reason") in {"pr_before_ruleset_effective_from", "pr_on_or_after_ruleset_effective_to"}
                    for res in per_ruleset.values()
                    if isinstance(res, dict)
                ):
                    if pr_num not in repo_rulesets_skipped_out_of_bounds_seen:
                        repo_rulesets_skipped_out_of_bounds_seen.add(pr_num)
                        if len(repo_rulesets_skipped_out_of_bounds) < max_pr_list:
                            repo_rulesets_skipped_out_of_bounds.append(pr_num)
                rebuilt_any = bool(
                    int(summary.get("created", 0) or 0)
                    or int(summary.get("updated", 0) or 0)
                    or int(summary.get("deleted", 0) or 0)
                )
                if stale_ruleset:
                    if rebuilt_any and pr_num not in repo_prs_rebuilt_stale_ruleset_seen:
                        repo_prs_rebuilt_stale_ruleset_seen.add(pr_num)
                        if len(repo_prs_rebuilt_stale_ruleset) < max_pr_list:
                            repo_prs_rebuilt_stale_ruleset.append(pr_num)
                record_queue_window_build_states(
                    pr=pr,
                    rule_sets=stale_rule_sets,
                    per_ruleset=per_ruleset,
                    revision_version=int(state.revision_version),
                    built_at=now_ts,
                )
                if rebuilt_any:
                    repo_rebuilt += 1

                repo_prs += 1
                total_prs += 1
                processed_pr_numbers.append(int(pr.number))

        batch: list[PullRequest] = []
        for pr in pr_qs:
            batch.append(pr)
            if len(batch) >= 200:
                _process_batch(batch)
                batch = []
                if repo_limit_hit:
                    break
        if not repo_limit_hit and batch:
            _process_batch(batch)
        total_rebuilt += repo_rebuilt
        total_prs_skipped_up_to_date += len(repo_prs_skipped_up_to_date_seen)
        total_rulesets_skipped_out_of_bounds += len(repo_rulesets_skipped_out_of_bounds_seen)
        total_prs_stale_ruleset += len(repo_prs_stale_ruleset_seen)
        total_prs_rebuilt_stale_ruleset += len(repo_prs_rebuilt_stale_ruleset_seen)
        per_repo.append(
            {
                "repo": f"{repo.owner}/{repo.name}",
                "prs_checked": repo_prs,
                "windows_rebuilt": repo_rebuilt,
                "prs_skipped_up_to_date": repo_prs_skipped_up_to_date,
                "rulesets_skipped_out_of_bounds": repo_rulesets_skipped_out_of_bounds,
                "prs_stale_ruleset": repo_prs_stale_ruleset,
                "prs_rebuilt_stale_ruleset": repo_prs_rebuilt_stale_ruleset,
                "limit_hit": repo_limit_hit,
            }
        )

    return {
        "repos": len(repos),
        "prs_checked": total_prs,
        "prs_checked_numbers": processed_pr_numbers,
        "windows_rebuilt": total_rebuilt,
        "prs_skipped_up_to_date": total_prs_skipped_up_to_date,
        "rulesets_skipped_out_of_bounds": total_rulesets_skipped_out_of_bounds,
        "prs_stale_ruleset": total_prs_stale_ruleset,
        "prs_rebuilt_stale_ruleset": total_prs_rebuilt_stale_ruleset,
        "only_complete_backfill": bool(only_complete_backfill),
        "per_repo": per_repo,
    }
