from __future__ import annotations

import logging
from typing import Any

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from analyzer.services.assignment_proposal_delivery import deliver_assignment_proposals
from core.models import Repository
from zulip_bot.services.zulip_client import ZulipApiError, ZulipClient

log = logging.getLogger(__name__)


@shared_task(name="analyzer.deliver_assignment_proposals")
def deliver_assignment_proposals_task(
    *,
    repository_id: int | None = None,
    include_inactive_repositories: bool = False,
    enabled_override: bool | None = None,
    dry_run_override: bool | None = None,
) -> dict[str, Any]:
    """Send the per-reviewer assignment-proposal digest DM (design doc 050, Chunk 5).

    One DM per ``confirm``-mode reviewer covering their pending, not-yet-notified proposals across
    all repositories, linking to the console. Dedupe is carried by ``AssignmentProposal.notified_at``
    (stamped after a successful send). Actual delivery requires BOTH the master
    ``ANALYZER_ASSIGNMENT_PROPOSALS_ENABLED`` and ``ANALYZER_ASSIGNMENT_PROPOSALS_DELIVERY_ENABLED``;
    ``ANALYZER_ASSIGNMENT_PROPOSALS_DRY_RUN`` computes the would-send set without any DM or stamp.
    """
    master_enabled = bool(getattr(settings, "ANALYZER_ASSIGNMENT_PROPOSALS_ENABLED", False))
    delivery_enabled = bool(getattr(settings, "ANALYZER_ASSIGNMENT_PROPOSALS_DELIVERY_ENABLED", False))
    enabled = bool(enabled_override) if enabled_override is not None else (master_enabled and delivery_enabled)
    dry_run = (
        bool(dry_run_override)
        if dry_run_override is not None
        else bool(getattr(settings, "ANALYZER_ASSIGNMENT_PROPOSALS_DRY_RUN", False))
    )

    if not enabled and not dry_run:
        return {
            "skipped": True,
            "reason": "feature_disabled",
            "enabled": enabled,
            "dry_run": dry_run,
            "master_enabled": master_enabled,
            "delivery_enabled": delivery_enabled,
        }

    repos_qs = Repository.objects.only("id", "owner", "name")
    if not include_inactive_repositories:
        repos_qs = repos_qs.filter(is_active=True)
    if repository_id is not None:
        repos_qs = repos_qs.filter(id=int(repository_id))
    repos = list(repos_qs.order_by("owner", "name", "id"))

    if repository_id is not None and not repos:
        return {"skipped": True, "reason": "repo_not_found_or_inactive", "repository_id": int(repository_id)}

    now = timezone.now()
    client: ZulipClient | None = None
    client_init_error: str | None = None
    if enabled and not dry_run:
        try:
            client = ZulipClient()
        except ZulipApiError as exc:
            client_init_error = str(exc)
            log.warning("analyzer.deliver_assignment_proposals: unable to initialize Zulip client: %s", client_init_error)

    try:
        delivery = deliver_assignment_proposals(repos, now=now, enabled=enabled, dry_run=dry_run, client=client)
    except Exception as exc:  # defensive: delivery failure must not crash the beat loop
        log.exception("analyzer.deliver_assignment_proposals: delivery failed")
        return {
            "skipped": False,
            "enabled": enabled,
            "dry_run": dry_run,
            "status": "error",
            "error": str(exc)[:2000],
            "repos": len(repos),
        }

    result = {
        "skipped": False,
        "enabled": enabled,
        "dry_run": dry_run,
        "master_enabled": master_enabled,
        "delivery_enabled": delivery_enabled,
        "include_inactive_repositories": bool(include_inactive_repositories),
        "repository_id": int(repository_id) if repository_id is not None else None,
        "repos": len(repos),
        "run_at": now.isoformat(),
        "client_init_error": client_init_error,
        "totals": delivery.get("stats", {}),
        "per_reviewer": delivery.get("per_reviewer", []),
    }
    stats = delivery.get("stats", {})
    log.info(
        "analyzer.deliver_assignment_proposals: repos=%s reviewers=%s sent=%s would_send=%s failed=%s "
        "notified=%s dry_run=%s enabled=%s",
        len(repos),
        stats.get("reviewers", 0),
        stats.get("sent", 0),
        stats.get("would_send", 0),
        stats.get("failed", 0),
        stats.get("proposals_notified", 0),
        dry_run,
        enabled,
    )
    return result


__all__ = ["deliver_assignment_proposals_task"]
