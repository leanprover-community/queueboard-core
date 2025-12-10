from __future__ import annotations

from celery import shared_task
from django.db import models
from django.utils import timezone

from analyzer.models import PRDependencyState
from analyzer.services.dependencies import PR_DEPENDENCY_BUILDER_VERSION, body_hash, rebuild_pr_dependencies
from core.models import Repository
from syncer.models import PullRequest
from syncer.models.pull_request import PullRequestState


@shared_task(name="analyzer.rebuild_pr_dependencies")
def rebuild_pr_dependencies_task(pr_id: int, *, builder_version: int = 1) -> dict:
    """Recompute PRDependency edges for a single PR."""
    pr = PullRequest.objects.select_related("repository").filter(id=int(pr_id)).first()
    if pr is None:
        return {"skipped": True, "reason": "pr_not_found"}

    result = rebuild_pr_dependencies(pr)
    repo = pr.repository
    state, _ = PRDependencyState.objects.get_or_create(pull_request=pr)
    state.last_checked_at = timezone.now()
    state.last_body_hash = body_hash(pr.body)
    state.builder_version = int(builder_version)
    state.save(update_fields=["last_checked_at", "last_body_hash", "builder_version", "updated_at"])
    return {
        "skipped": False,
        "repo": f"{repo.owner}/{repo.name}",
        "pr_number": int(pr.number),
        "repo_pr": f"{repo.owner}/{repo.name}#{int(pr.number)}",
        "created": int(result.created),
        "updated": int(result.updated),
        "deleted": int(result.deleted),
        "parsed_numbers": result.parsed_numbers,
        "resolved_numbers": result.resolved_numbers,
        "unresolved_numbers": result.unresolved_numbers,
    }


@shared_task(name="analyzer.rebuild_dependencies_sweep")
def rebuild_dependencies_sweep_task(
    *,
    max_prs_per_repo: int = 200,
    only_open: bool = True,
    builder_version: int = PR_DEPENDENCY_BUILDER_VERSION,
    fanout: bool = False,
) -> dict:
    """Sweep active repositories and rebuild PRDependency edges from PR bodies.

    Processes PRs in least-recently-checked order to ensure gradual coverage.
    """
    repos = list(Repository.objects.filter(is_active=True).only("id", "owner", "name"))
    total_created = 0
    total_updated = 0
    total_deleted = 0
    total_prs = 0
    total_enqueued = 0
    processed_pr_numbers: list[int] = []
    per_repo: list[dict] = []

    for repo in repos:
        pr_qs = (
            PullRequest.objects.filter(repository=repo)
            .select_related("repository", "dependency_state")
            .only(
                "id",
                "number",
                "body",
                "state",
                "gh_updated_at",
                "repository",
                "repository__owner",
                "repository__name",
                "dependency_state__builder_version",
                "dependency_state__last_checked_at",
                "dependency_state__last_body_hash",
            )
        )
        if only_open:
            pr_qs = pr_qs.filter(state=PullRequestState.OPEN)
        pr_qs = pr_qs.annotate(
            is_open_flag=models.Case(
                models.When(state=PullRequestState.OPEN, then=models.Value(1)),
                default=models.Value(0),
                output_field=models.IntegerField(),
            )
        )
        pr_qs = (
            pr_qs.filter(
                models.Q(dependency_state__builder_version=builder_version)
                | models.Q(dependency_state__builder_version__isnull=True)
            )
            .order_by("dependency_state__last_checked_at", "-is_open_flag", "-gh_updated_at", "-id")
            .iterator(chunk_size=100)
        )

        repo_created = 0
        repo_updated = 0
        repo_deleted = 0
        repo_prs = 0
        repo_enqueued = 0
        for pr in pr_qs:
            if repo_prs >= int(max_prs_per_repo):
                break
            repo_prs += 1
            processed_pr_numbers.append(int(pr.number))
            total_prs += 1
            if fanout:
                async_res = rebuild_pr_dependencies_task.delay(pr.id, builder_version=builder_version)
                repo_enqueued += 1
                total_enqueued += 1
                continue

            res = rebuild_pr_dependencies(pr)
            repo_created += res.created
            repo_updated += res.updated
            repo_deleted += res.deleted
            state, _ = PRDependencyState.objects.get_or_create(pull_request=pr)
            state.last_checked_at = timezone.now()
            state.last_body_hash = body_hash(pr.body)
            state.builder_version = int(builder_version)
            state.save(update_fields=["last_checked_at", "last_body_hash", "builder_version", "updated_at"])
        total_created += repo_created
        total_updated += repo_updated
        total_deleted += repo_deleted
        per_repo.append(
            {
                "repo": f"{repo.owner}/{repo.name}",
                "prs_processed": repo_prs,
                "created": repo_created,
                "updated": repo_updated,
                "deleted": repo_deleted,
                "enqueued": repo_enqueued,
            }
        )

    return {
        "repos": len(repos),
        "prs_processed": total_prs,
        "created": total_created,
        "updated": total_updated,
        "deleted": total_deleted,
        "enqueued": total_enqueued,
        "prs_processed_numbers": processed_pr_numbers,
        "only_open": bool(only_open),
        "max_prs_per_repo": int(max_prs_per_repo),
        "builder_version": int(builder_version),
        "fanout": bool(fanout),
        "per_repo": per_repo,
    }
