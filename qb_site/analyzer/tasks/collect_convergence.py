from __future__ import annotations

from celery import shared_task
from django.utils import timezone
from django.db.models import Exists, OuterRef, F, Q

from analyzer.models import AnalyzerConvergenceSnapshot, PRQueueWindow, PRQueueWindowBuildState, PRRevision, QueueRuleSet
from analyzer.services.dependencies import PR_DEPENDENCY_BUILDER_VERSION
from core.models import Repository
from syncer.models import CheckRun, CIShaFetchState, PullRequest, StatusContext


@shared_task(name="analyzer.collect_convergence")
def collect_analyzer_convergence_task() -> dict:
    """Collect analyzer convergence counts per active repository."""
    collected_at = timezone.now()
    repos = list(Repository.objects.filter(is_active=True).only("id", "owner", "name"))
    rows = 0
    per_repo: list[dict] = []
    for repo in repos:
        rulesets = list(QueueRuleSet.objects.filter(repository=repo))
        active_rulesets = [rs for rs in rulesets if rs.is_active]
        base_prs = PullRequest.objects.filter(repository=repo, timeline_backfill_done=True)

        pr_no_revisions = (
            base_prs.annotate(has_rev=Exists(PRRevision.objects.filter(pull_request=OuterRef("pk"))))
            .filter(has_rev=False)
            .count()
        )

        prs_with_rev = list(
            base_prs.annotate(
                rev_version=F("revision_build_state__revision_version"),
                windows_rev=F("revision_build_state__windows_built_revision_version"),
                windows_at=F("revision_build_state__windows_built_at"),
            )
            .filter(rev_version__isnull=False)
            .values("id", "rev_version", "windows_rev", "windows_at")
        )
        windows_stale = 0
        if active_rulesets and prs_with_rev:
            pr_ids = [int(pr["id"]) for pr in prs_with_rev]
            rule_set_ids = [int(rs.id) for rs in active_rulesets]
            rs_state_rows = PRQueueWindowBuildState.objects.filter(
                pull_request_id__in=pr_ids,
                rule_set_id__in=rule_set_ids,
            ).values("pull_request_id", "rule_set_id", "revision_version_built", "windows_built_at")
            rs_state_map = {(int(row["pull_request_id"]), int(row["rule_set_id"])): row for row in rs_state_rows}
            rollup_stale_pairs = set(
                (
                    int(row["pull_request_id"]),
                    int(row["rule_set_id"]),
                )
                for row in PRQueueWindow.objects.filter(
                    pull_request_id__in=pr_ids,
                    rule_set_id__in=rule_set_ids,
                )
                .filter(Q(window_count=0) | Q(first_on_queue_ts__isnull=True))
                .values("pull_request_id", "rule_set_id")
                .distinct()
            )

            for pr_row in prs_with_rev:
                pr_id = int(pr_row["id"])
                rev_version = int(pr_row["rev_version"])
                legacy_windows_rev = pr_row["windows_rev"]
                legacy_windows_at = pr_row["windows_at"]
                for rs in active_rulesets:
                    rs_id = int(rs.id)
                    if (pr_id, rs_id) in rollup_stale_pairs:
                        windows_stale += 1
                        continue
                    rs_state = rs_state_map.get((pr_id, rs_id))
                    if rs_state is not None:
                        rs_rev = rs_state["revision_version_built"]
                        rs_built_at = rs_state["windows_built_at"]
                        stale = (
                            rs_rev is None
                            or int(rs_rev) < rev_version
                            or rs_built_at is None
                            or (bool(rs.updated_at) and bool(rs_built_at) and rs_built_at < rs.updated_at)
                        )
                        if stale:
                            windows_stale += 1
                        continue

                    # Transitional fallback when per-ruleset state is missing.
                    legacy_stale = (
                        legacy_windows_rev is None
                        or int(legacy_windows_rev) < rev_version
                        or legacy_windows_at is None
                        or (bool(rs.updated_at) and bool(legacy_windows_at) and legacy_windows_at < rs.updated_at)
                    )
                    if legacy_stale:
                        windows_stale += 1

        # Count revision heads whose CI has not been checked:
        # - no CI rows for this PR/head_sha
        # - and no CIShaFetchState for the repo/head_sha
        rev_qs = (
            PRRevision.objects.filter(pull_request__repository=repo, pull_request__timeline_backfill_done=True)
            .exclude(head_sha__isnull=True)
            .exclude(head_sha="")
        )
        head_cr = CheckRun.objects.filter(pull_request=OuterRef("pull_request_id"), head_sha=OuterRef("head_sha"))
        head_sc = StatusContext.objects.filter(pull_request=OuterRef("pull_request_id"), head_sha=OuterRef("head_sha"))
        head_fetch = CIShaFetchState.objects.filter(repository=repo, sha=OuterRef("head_sha"))
        ci_not_checked = (
            rev_qs.annotate(has_cr=Exists(head_cr), has_sc=Exists(head_sc), has_fetch=Exists(head_fetch))
            .filter(has_cr=False, has_sc=False, has_fetch=False)
            .count()
        )

        ci_gated_missing_windows = 0
        ci_rulesets = [rs for rs in active_rulesets if rs.require_ci_success]
        if ci_rulesets:
            prs_with_rev = base_prs.annotate(has_rev=Exists(PRRevision.objects.filter(pull_request=OuterRef("pk")))).filter(
                has_rev=True
            )
            for rs in ci_rulesets:
                rs_windows = PRQueueWindow.objects.filter(pull_request=OuterRef("pk"), rule_set=rs)
                if rs.effective_from:
                    rs_windows = rs_windows.filter(from_ts__gte=rs.effective_from)
                if rs.effective_to:
                    rs_windows = rs_windows.filter(from_ts__lt=rs.effective_to)
                missing = prs_with_rev.annotate(has_win=Exists(rs_windows)).filter(has_win=False).count()
                ci_gated_missing_windows += missing

        missing_rollups = (
            PRQueueWindow.objects.filter(pull_request__repository=repo)
            .filter(Q(window_count=0) | Q(first_on_queue_ts__isnull=True))
            .values("pull_request_id")
            .distinct()
            .count()
        )

        dep_missing = base_prs.filter(dependency_state__isnull=True).count()
        dep_stale = (
            base_prs.filter(dependency_state__isnull=False).filter(
                Q(dependency_state__builder_version__lt=PR_DEPENDENCY_BUILDER_VERSION)
                | Q(dependency_state__last_checked_at__isnull=True)
                | Q(dependency_state__last_checked_at__lt=F("last_synced_at"))
                | Q(last_synced_at__isnull=True)
            )
        ).count()

        AnalyzerConvergenceSnapshot.objects.create(
            repository=repo,
            collected_at=collected_at,
            pr_no_revisions=pr_no_revisions,
            windows_stale=windows_stale,
            ci_not_checked=ci_not_checked,
            ci_gated_missing_windows=ci_gated_missing_windows,
            prs_missing_queue_window_rollups=missing_rollups,
            prs_missing_dependency_state=dep_missing,
            prs_stale_dependency_state=dep_stale,
        )
        rows += 1
        per_repo.append(
            {
                "repo": f"{repo.owner}/{repo.name}",
                "pr_no_revisions": pr_no_revisions,
                "windows_stale": windows_stale,
                "ci_not_checked": ci_not_checked,
                "ci_gated_missing_windows": ci_gated_missing_windows,
                "prs_missing_queue_window_rollups": missing_rollups,
                "prs_missing_dependency_state": dep_missing,
                "prs_stale_dependency_state": dep_stale,
            }
        )

    return {"repos": len(repos), "rows_created": rows, "per_repo": per_repo}
