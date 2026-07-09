from __future__ import annotations

import logging
from typing import Any

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from analyzer.services.reviewer_assignment_propose import propose_assignments_for_repo
from core.models import Repository

log = logging.getLogger(__name__)


def _aggregate_key(totals: dict[str, Any], stats: dict[str, Any]) -> None:
    for key, value in stats.items():
        if key == "capped":
            totals["capped"] = totals.get("capped", False) or bool(value)
            continue
        totals[key] = int(totals.get(key, 0)) + int(value)


@shared_task(name="analyzer.propose_reviewer_assignments")
def propose_reviewer_assignments_task(
    *,
    repository_id: int | None = None,
    include_inactive_repositories: bool = False,
    enabled_override: bool | None = None,
    dry_run_override: bool | None = None,
) -> dict[str, Any]:
    """Propose reviewer assignments through the acceptance gate for all active repositories.

    Per ``(pr_number -> reviewer_login)`` from each repo's authoritative default-rule-set snapshot,
    branch on the reviewer's ``assignment_acceptance`` mode: ``auto`` (or ``confirm`` with no
    reachable Zulip channel) direct-assigns via the ``assign_pr`` operation token exactly like the
    legacy apply task; ``confirm`` with a Zulip link creates an ``AssignmentProposal`` awaiting
    acceptance. Gated by ``ANALYZER_ASSIGNMENT_PROPOSALS_ENABLED`` (+ dry-run); a cheap no-op
    otherwise. This supersedes ``analyzer.apply_reviewer_assignments`` — run one or the other.
    """
    enabled = (
        bool(enabled_override)
        if enabled_override is not None
        else bool(getattr(settings, "ANALYZER_ASSIGNMENT_PROPOSALS_ENABLED", False))
    )
    dry_run = (
        bool(dry_run_override)
        if dry_run_override is not None
        else bool(getattr(settings, "ANALYZER_ASSIGNMENT_PROPOSALS_DRY_RUN", False))
    )

    if not enabled and not dry_run:
        return {"skipped": True, "reason": "feature_disabled", "enabled": enabled, "dry_run": dry_run}

    window_days = int(getattr(settings, "ANALYZER_ASSIGNMENT_PROPOSAL_WINDOW_DAYS", 7))
    # Reuse the apply task's mutation-safety knobs for the direct-assign (auto/fallback) path so the
    # two rollout modes bound GitHub exposure identically.
    dedupe_days = int(getattr(settings, "ANALYZER_REVIEWER_ASSIGNMENT_APPLY_DEDUPE_DAYS", 7))
    max_age_hours = int(getattr(settings, "ANALYZER_REVIEWER_ASSIGNMENT_APPLY_MAX_AGE_HOURS", 48))
    max_per_repo = int(getattr(settings, "ANALYZER_REVIEWER_ASSIGNMENT_APPLY_MAX_PER_REPO", 25))

    repos_qs = Repository.objects.only("id", "owner", "name")
    if not include_inactive_repositories:
        repos_qs = repos_qs.filter(is_active=True)
    if repository_id is not None:
        repos_qs = repos_qs.filter(id=int(repository_id))
    repos = list(repos_qs.order_by("owner", "name", "id"))

    if repository_id is not None and not repos:
        return {"skipped": True, "reason": "repo_not_found_or_inactive", "repository_id": int(repository_id)}

    now = timezone.now()
    run_date = now.date()
    per_repo: list[dict[str, Any]] = []
    totals: dict[str, Any] = {}
    repos_errored = 0

    for repo in repos:
        try:
            repo_result = propose_assignments_for_repo(
                repo,
                run_date=run_date,
                now=now,
                enabled=enabled,
                dry_run=dry_run,
                window_days=window_days,
                dedupe_days=dedupe_days,
                max_age_hours=max_age_hours,
                max_per_repo=max_per_repo,
            )
        except Exception as exc:  # defensive: one repo's failure must not abort the whole sweep
            repos_errored += 1
            log.exception(
                "analyzer.propose_reviewer_assignments: repo failed repo=%s/%s",
                repo.owner,
                repo.name,
            )
            repo_result = {
                "repo": f"{repo.owner}/{repo.name}",
                "repo_id": int(repo.id),
                "status": "error",
                "error": str(exc)[:2000],
                "stats": {},
            }
        per_repo.append(repo_result)
        _aggregate_key(totals, repo_result.get("stats", {}))

    result = {
        "skipped": False,
        "enabled": enabled,
        "dry_run": dry_run,
        "include_inactive_repositories": bool(include_inactive_repositories),
        "repository_id": int(repository_id) if repository_id is not None else None,
        "repos": len(repos),
        "repos_errored": repos_errored,
        "run_date": run_date.isoformat(),
        "window_days": window_days,
        "dedupe_days": dedupe_days,
        "max_age_hours": max_age_hours,
        "max_per_repo": max_per_repo,
        "totals": totals,
        "per_repo": per_repo,
    }

    log.info(
        "analyzer.propose_reviewer_assignments: repos=%s errored=%s proposed=%s assigned=%s failed=%s dry_run=%s enabled=%s",
        len(repos),
        repos_errored,
        totals.get("proposed", 0),
        totals.get("assigned_auto", 0) + totals.get("assigned_fallback", 0),
        totals.get("failed", 0),
        dry_run,
        enabled,
    )
    return result


__all__ = ["propose_reviewer_assignments_task"]
