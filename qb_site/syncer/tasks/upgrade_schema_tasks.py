"""Celery tasks driving the sync_schema_version upgrader.

See ``docs/design-decisions/044-sync-schema-versioning-and-comment-review-timeline-events.md``
and ``qb_site/syncer/services/sync_schema_upgrades.py``.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from celery import shared_task
from django.conf import settings

from core.models import Repository
from syncer.models import PullRequest
from syncer.services.sync_schema_upgrades import (
    CURRENT_SYNC_SCHEMA_VERSION,
    DispatchOutcome,
    dispatch,
    effective_target_version,
)


@shared_task(name="syncer.upgrade_schema_versions")
def upgrade_schema_versions_task(  # type: ignore[no-redef]
    repo_id: int,
    *,
    batch_size: Optional[int] = None,
    kick_limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Advance ``PullRequest.sync_schema_version`` toward ``CURRENT_SYNC_SCHEMA_VERSION``.

    Pulls up to ``batch_size`` PRs in the repo at
    ``sync_schema_version < CURRENT_SYNC_SCHEMA_VERSION``, ordered so older /
    further-behind PRs are processed first, and walks each through the
    upgrader dispatcher. Kicks share a single ``kick_limit`` budget across
    the batch; auto-stamps and ``is_complete=True`` stamps are unbounded
    because they are DB-only.
    """
    repo = Repository.objects.get(id=int(repo_id))

    eff_batch = int(batch_size) if batch_size is not None else int(getattr(settings, "SYNCER_SCHEMA_UPGRADE_BATCH_SIZE", 1000))
    eff_kick_limit = int(kick_limit) if kick_limit is not None else int(getattr(settings, "SYNCER_SCHEMA_UPGRADE_KICK_LIMIT", 20))
    eff_target = effective_target_version()
    if eff_batch <= 0:
        return {
            "repo": f"{repo.owner}/{repo.name}",
            "repo_id": repo.id,
            "considered": 0,
            "stamped": 0,
            "kicked": 0,
            "target": eff_target,
            "current": CURRENT_SYNC_SCHEMA_VERSION,
        }

    candidates = list(
        PullRequest.objects.filter(repository=repo, sync_schema_version__lt=eff_target).order_by(
            "sync_schema_version", "-gh_updated_at", "-id"
        )[:eff_batch]
    )

    stamped = 0
    kicked = 0
    auto_stamped = 0
    kick_budget = max(0, eff_kick_limit)
    for pr in candidates:
        outcome: DispatchOutcome = dispatch(pr, kick_budget=kick_budget, target_version=eff_target)
        if outcome.stamped_to is not None:
            stamped += 1
        if outcome.auto_stamped_versions:
            auto_stamped += len(outcome.auto_stamped_versions)
        if outcome.kicked:
            kicked += 1
            kick_budget = max(0, kick_budget - 1)

    return {
        "repo": f"{repo.owner}/{repo.name}",
        "repo_id": repo.id,
        "considered": len(candidates),
        "stamped": stamped,
        "auto_stamped": auto_stamped,
        "kicked": kicked,
        "kick_budget_remaining": kick_budget,
        "target": eff_target,
        "current": CURRENT_SYNC_SCHEMA_VERSION,
    }


@shared_task(name="syncer.upgrade_schema_versions_active")
def upgrade_schema_versions_active_task(  # type: ignore[no-redef]
    *,
    batch_size: Optional[int] = None,
    kick_limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Fan out :func:`upgrade_schema_versions_task` over all active repos."""
    repos = list(Repository.objects.filter(is_active=True).only("id", "owner", "name"))
    enqueued = 0
    for repo in repos:
        upgrade_schema_versions_task.delay(repo.id, batch_size=batch_size, kick_limit=kick_limit)
        enqueued += 1
    return {
        "repos": len(repos),
        "enqueued": enqueued,
        "batch_size": batch_size,
        "kick_limit": kick_limit,
        "target": effective_target_version(),
        "current": CURRENT_SYNC_SCHEMA_VERSION,
    }
