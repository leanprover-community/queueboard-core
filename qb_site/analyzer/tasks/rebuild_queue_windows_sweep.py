from __future__ import annotations

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from analyzer.models import QueueRuleSet, PRRevisionBuildState, PRRevision
from analyzer.services.queue_windows import rebuild_queue_windows_for_ruleset
from core.models import Repository
from syncer.models import PullRequest


@shared_task(name="analyzer.rebuild_queue_windows_sweep")
def rebuild_queue_windows_sweep_task(
    *,
    max_prs_per_repo: int = 50,
    only_complete_backfill: bool = False,
) -> dict:
    """Rebuild queue windows for PRs whose revision_version changed or windows are stale."""
    now_ts = timezone.now()
    repos = list(Repository.objects.filter(is_active=True).only("id", "owner", "name"))
    total_rebuilt = 0
    total_prs = 0
    per_repo: list[dict] = []

    for repo in repos:
        pr_qs = (
            PullRequest.objects.filter(repository=repo, timeline_backfill_done=True)
            .select_related("revision_build_state")
            .only(
                "id",
                "number",
                "gh_created_at",
                "gh_updated_at",
                "timeline_backfill_done",
                "commits_backfill_done",
                "revision_build_state__revision_version",
                "revision_build_state__windows_built_revision_version",
                "revision_build_state__windows_built_at",
            )
            .order_by("-gh_updated_at", "-id")
            .iterator(chunk_size=100)
        )
        if only_complete_backfill:
            pr_qs = (p for p in pr_qs if p.commits_backfill_done)
        repo_rebuilt = 0
        repo_prs = 0

        rulesets = list(QueueRuleSet.objects.filter(repository=repo))
        if not rulesets:
            per_repo.append({"repo": f"{repo.owner}/{repo.name}", "prs_checked": 0, "windows_rebuilt": 0})
            continue

        for pr in pr_qs:
            if repo_prs >= int(max_prs_per_repo):
                break

            state, _ = PRRevisionBuildState.objects.get_or_create(pull_request=pr)
            # Skip if windows already built for current revision_version and not stale vs ruleset updates.
            stale_ruleset = False
            for rs in rulesets:
                if state.windows_built_at and rs.updated_at and state.windows_built_at < rs.updated_at:
                    stale_ruleset = True
                    break
            if (
                state.windows_built_revision_version is not None
                and state.windows_built_revision_version == state.revision_version
                and not stale_ruleset
            ):
                continue

            if not PRRevision.objects.filter(pull_request=pr).exists():
                continue

            rebuilt_any = False
            for rs in rulesets:
                created_at = pr.gh_created_at
                if rs.effective_from and created_at < rs.effective_from:
                    continue
                if rs.effective_to and created_at >= rs.effective_to:
                    continue
                res = rebuild_queue_windows_for_ruleset(pr=pr, rule_set=rs)
                if res.created or res.updated or res.deleted:
                    rebuilt_any = True
            if rebuilt_any:
                state.windows_built_revision_version = state.revision_version
                state.windows_built_at = now_ts
                state.save(update_fields=["windows_built_revision_version", "windows_built_at", "updated_at"])
                repo_rebuilt += 1

            repo_prs += 1
            total_prs += 1
        total_rebuilt += repo_rebuilt
        per_repo.append({"repo": f"{repo.owner}/{repo.name}", "prs_checked": repo_prs, "windows_rebuilt": repo_rebuilt})

    return {
        "repos": len(repos),
        "prs_checked": total_prs,
        "windows_rebuilt": total_rebuilt,
        "only_complete_backfill": bool(only_complete_backfill),
        "per_repo": per_repo,
    }
