from __future__ import annotations

import logging
from typing import Any

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from analyzer.services.reviewer_assignment_apply import apply_assignments_for_repo
from core.models import Repository

log = logging.getLogger(__name__)


def _aggregate_key(totals: dict[str, Any], stats: dict[str, Any]) -> None:
    for key, value in stats.items():
        if key == "capped":
            totals["capped"] = totals.get("capped", False) or bool(value)
            continue
        totals[key] = int(totals.get(key, 0)) + int(value)


@shared_task(name="analyzer.apply_reviewer_assignments")
def apply_reviewer_assignments_task(
    *,
    repository_id: int | None = None,
    include_inactive_repositories: bool = False,
    enabled_override: bool | None = None,
    dry_run_override: bool | None = None,
) -> dict[str, Any]:
    """Apply proposed reviewer assignments to GitHub for all active repositories.

    Reads each repo's authoritative default-rule-set ``ReviewerAssignmentSnapshot``
    and POSTs the proposed assignees via the ``assign_pr`` operation token, recording
    every outcome in ``ReviewerAssignmentApplication``. Gated by
    ``ANALYZER_REVIEWER_ASSIGNMENT_APPLY_ENABLED`` (and an optional dry-run mode); when
    neither enabled nor dry-run, the task is a cheap no-op.
    """
    enabled = (
        bool(enabled_override)
        if enabled_override is not None
        else bool(getattr(settings, "ANALYZER_REVIEWER_ASSIGNMENT_APPLY_ENABLED", False))
    )
    dry_run = (
        bool(dry_run_override)
        if dry_run_override is not None
        else bool(getattr(settings, "ANALYZER_REVIEWER_ASSIGNMENT_APPLY_DRY_RUN", False))
    )

    if not enabled and not dry_run:
        return {"skipped": True, "reason": "feature_disabled", "enabled": enabled, "dry_run": dry_run}

    dedupe_days = int(getattr(settings, "ANALYZER_REVIEWER_ASSIGNMENT_APPLY_DEDUPE_DAYS", 7))
    max_age_hours = int(getattr(settings, "ANALYZER_REVIEWER_ASSIGNMENT_APPLY_MAX_AGE_HOURS", 48))
    max_per_repo = int(getattr(settings, "ANALYZER_REVIEWER_ASSIGNMENT_APPLY_MAX_PER_REPO", 0))

    repos_qs = Repository.objects.only("id", "owner", "name")
    if not include_inactive_repositories:
        repos_qs = repos_qs.filter(is_active=True)
    if repository_id is not None:
        repos_qs = repos_qs.filter(id=int(repository_id))
    repos = list(repos_qs.order_by("owner", "name", "id"))

    if repository_id is not None and not repos:
        return {
            "skipped": True,
            "reason": "repo_not_found_or_inactive",
            "repository_id": int(repository_id),
        }

    now = timezone.now()
    run_date = now.date()
    per_repo: list[dict[str, Any]] = []
    totals: dict[str, Any] = {}

    for repo in repos:
        repo_result = apply_assignments_for_repo(
            repo,
            run_date=run_date,
            now=now,
            enabled=enabled,
            dry_run=dry_run,
            dedupe_days=dedupe_days,
            max_age_hours=max_age_hours,
            max_per_repo=max_per_repo,
        )
        per_repo.append(repo_result)
        _aggregate_key(totals, repo_result.get("stats", {}))

    result = {
        "skipped": False,
        "enabled": enabled,
        "dry_run": dry_run,
        "include_inactive_repositories": bool(include_inactive_repositories),
        "repository_id": int(repository_id) if repository_id is not None else None,
        "repos": len(repos),
        "run_date": run_date.isoformat(),
        "dedupe_days": dedupe_days,
        "max_age_hours": max_age_hours,
        "max_per_repo": max_per_repo,
        "totals": totals,
        "per_repo": per_repo,
    }

    log.info(
        "analyzer.apply_reviewer_assignments: repos=%s applied=%s failed=%s dry_run=%s enabled=%s",
        len(repos),
        totals.get("applied", 0),
        totals.get("failed", 0),
        dry_run,
        enabled,
    )
    return result


__all__ = ["apply_reviewer_assignments_task"]
