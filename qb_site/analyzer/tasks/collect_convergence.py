from __future__ import annotations

from celery import shared_task
from django.utils import timezone
from django.db.models import Exists, OuterRef, F, Max, Q

from analyzer.models import AnalyzerConvergenceSnapshot, PRRevision, PRRevisionBuildState, PRQueueWindow, QueueRuleSet
from analyzer.services.dependencies import PR_DEPENDENCY_BUILDER_VERSION
from core.models import Repository
from syncer.models import PullRequest


@shared_task(name="analyzer.collect_convergence")
def collect_analyzer_convergence_task() -> dict:
    """Collect analyzer convergence counts per active repository."""
    collected_at = timezone.now()
    repos = list(Repository.objects.filter(is_active=True).only("id", "owner", "name"))
    rows = 0
    per_repo: list[dict] = []
    for repo in repos:
        rulesets = list(QueueRuleSet.objects.filter(repository=repo))
        ruleset_updated_at = QueueRuleSet.objects.filter(repository=repo).aggregate(m=Max("updated_at")).get("m")
        base_prs = PullRequest.objects.filter(repository=repo, timeline_backfill_done=True)

        pr_no_revisions = (
            base_prs.annotate(has_rev=Exists(PRRevision.objects.filter(pull_request=OuterRef("pk"))))
            .filter(has_rev=False)
            .count()
        )

        windows_stale = (
            base_prs.annotate(
                rev_version=F("revision_build_state__revision_version"),
                windows_rev=F("revision_build_state__windows_built_revision_version"),
                windows_at=F("revision_build_state__windows_built_at"),
            )
            .filter(rev_version__isnull=False)
            .filter(
                Q(windows_rev__isnull=True)
                | Q(windows_rev__lt=F("rev_version"))
                | (Q(windows_at__lt=ruleset_updated_at) if ruleset_updated_at else Q(pk__isnull=False))
            )
            .count()
        )

        ci_not_checked = (
            PRRevisionBuildState.objects.filter(
                pull_request__repository=repo,
                pull_request__timeline_backfill_done=True,
                revision_version__gt=0,
            )
            .exclude(ci_checked_revision_version=F("revision_version"))
            .count()
        )

        ci_gated_missing_windows = 0
        ci_rulesets = [rs for rs in rulesets if rs.require_ci_success]
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
