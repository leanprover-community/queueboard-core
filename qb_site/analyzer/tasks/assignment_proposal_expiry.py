from __future__ import annotations

import logging
from typing import Any

from celery import shared_task
from django.utils import timezone

from analyzer.services.assignment_proposal_expiry import expire_and_reconcile_proposals_for_repo
from core.models import Repository

log = logging.getLogger(__name__)


@shared_task(name="analyzer.expire_assignment_proposals")
def expire_assignment_proposals_task(
    *,
    repository_id: int | None = None,
    include_inactive_repositories: bool = False,
) -> dict[str, Any]:
    """Expire timed-out proposals and supersede those whose PR left the queue (design doc 050).

    Essential maintenance for the acceptance gate: it is intentionally NOT gated by the master
    switch, so existing proposals keep draining even after the gate is turned off. Performs no
    GitHub writes; only transitions ``AssignmentProposal`` state via the ``proposal_validity``
    authority. Idempotent and safe to run frequently.
    """
    repos_qs = Repository.objects.only("id", "owner", "name")
    if not include_inactive_repositories:
        repos_qs = repos_qs.filter(is_active=True)
    if repository_id is not None:
        repos_qs = repos_qs.filter(id=int(repository_id))
    repos = list(repos_qs.order_by("owner", "name", "id"))

    if repository_id is not None and not repos:
        return {"skipped": True, "reason": "repo_not_found_or_inactive", "repository_id": int(repository_id)}

    now = timezone.now()
    per_repo: list[dict[str, Any]] = []
    totals: dict[str, int] = {}
    repos_errored = 0

    for repo in repos:
        try:
            repo_result = expire_and_reconcile_proposals_for_repo(repo, now=now)
        except Exception as exc:  # defensive: one repo's failure must not abort the whole sweep
            repos_errored += 1
            log.exception("analyzer.expire_assignment_proposals: repo failed repo=%s/%s", repo.owner, repo.name)
            repo_result = {
                "repo": f"{repo.owner}/{repo.name}",
                "repo_id": int(repo.id),
                "status": "error",
                "error": str(exc)[:2000],
                "stats": {},
            }
        per_repo.append(repo_result)
        for key, value in repo_result.get("stats", {}).items():
            totals[key] = int(totals.get(key, 0)) + int(value)

    result = {
        "skipped": False,
        "include_inactive_repositories": bool(include_inactive_repositories),
        "repository_id": int(repository_id) if repository_id is not None else None,
        "repos": len(repos),
        "repos_errored": repos_errored,
        "run_at": now.isoformat(),
        "totals": totals,
        "per_repo": per_repo,
    }
    log.info(
        "analyzer.expire_assignment_proposals: repos=%s errored=%s expired=%s superseded=%s still_live=%s",
        len(repos),
        repos_errored,
        totals.get("expired", 0),
        totals.get("superseded", 0),
        totals.get("still_live", 0),
    )
    return result


__all__ = ["expire_assignment_proposals_task"]
