from __future__ import annotations

import logging
from typing import Any

from celery import shared_task
from django.conf import settings

from analyzer.services import build_reviewer_attention_reports
from core.models import Repository


log = logging.getLogger(__name__)


def _derive_new_assignment_ping_window_seconds() -> tuple[int, str]:
    """Derive 'new assignment' window from reviewer-attention sweep schedule."""
    has_fixed_utc_clock = (
        getattr(settings, "ANALYZER_REVIEWER_ATTENTION_UTC_HOUR", None) is not None
        or getattr(settings, "ANALYZER_REVIEWER_ATTENTION_UTC_MINUTE", None) is not None
    )
    if has_fixed_utc_clock:
        return (24 * 60 * 60, "fixed_utc_clock")

    period_seconds = int(getattr(settings, "ANALYZER_REVIEWER_ATTENTION_PERIOD_SECONDS", 86400))
    if period_seconds > 0:
        return (period_seconds, "period_seconds")
    return (24 * 60 * 60, "fallback_24h")


@shared_task(name="analyzer.reviewer_attention_daily")
def reviewer_attention_daily_task(*, repository_id: int | None = None) -> dict[str, Any]:
    """Run daily reviewer-attention policy sweep in dry-run mode.

    This task is intentionally read-only for now:
    - computes nudge/auto-unassign eligibility,
    - emits structured summary payloads,
    - does not send notifications or mutate GitHub assignments yet.
    """

    reports_enabled = bool(getattr(settings, "ANALYZER_REVIEWER_ATTENTION_ENABLED", False))
    enforcement_enabled = bool(getattr(settings, "ANALYZER_REVIEWER_ATTENTION_ENFORCEMENT_ENABLED", False))
    new_assignment_ping_window_seconds, new_assignment_ping_window_source = _derive_new_assignment_ping_window_seconds()

    if not reports_enabled:
        return {
            "skipped": True,
            "reason": "feature_disabled",
            "reports_enabled": reports_enabled,
            "enforcement_enabled": enforcement_enabled,
        }

    repos_qs = Repository.objects.filter(is_active=True).only("id", "owner", "name")
    if repository_id is not None:
        repos_qs = repos_qs.filter(id=int(repository_id))

    repos = list(repos_qs.order_by("owner", "name", "id"))
    if repository_id is not None and not repos:
        return {
            "skipped": True,
            "reason": "repo_not_found_or_inactive",
            "repository_id": int(repository_id),
            "reports_enabled": reports_enabled,
            "enforcement_enabled": enforcement_enabled,
        }

    repos_summary: list[dict[str, Any]] = []
    totals = {
        "reviewers": 0,
        "reviewers_with_notifications_enabled": 0,
        "reviewers_with_events": 0,
        "reviewers_to_notify": 0,
        "assigned_items": 0,
        "would_nudge": 0,
        "would_auto_unassign": 0,
        "would_new_assignment_ping": 0,
        "missing_assignment_timestamps": 0,
        "warnings": 0,
    }

    for repo in repos:
        reports = build_reviewer_attention_reports(
            repository=repo,
            new_assignment_ping_window_seconds=new_assignment_ping_window_seconds,
        )

        reviewers = len(reports)
        reviewers_with_notifications = sum(1 for report in reports if report.notifications_enabled)
        reviewers_with_events = sum(1 for report in reports if report.has_events_of_interest)
        reviewers_to_notify = sum(1 for report in reports if report.has_notifications_to_send)
        assigned_items = sum(len(report.items) for report in reports)
        would_nudge = sum(1 for report in reports for item in report.items if item.needs_nudge)
        would_auto_unassign = sum(1 for report in reports for item in report.items if item.needs_auto_unassign)
        would_new_assignment_ping = sum(1 for report in reports for item in report.items if item.needs_new_assignment_ping)
        missing_assignment_timestamps = sum(1 for report in reports for item in report.items if item.missing_assignment_timestamp)
        warnings = sum(len(report.warnings) for report in reports)

        repo_payload = {
            "repo": f"{repo.owner}/{repo.name}",
            "repo_id": int(repo.id),
            "reviewers": reviewers,
            "reviewers_with_notifications_enabled": reviewers_with_notifications,
            "reviewers_with_events": reviewers_with_events,
            "reviewers_to_notify": reviewers_to_notify,
            "assigned_items": assigned_items,
            "would_nudge": would_nudge,
            "would_auto_unassign": would_auto_unassign,
            "would_new_assignment_ping": would_new_assignment_ping,
            "missing_assignment_timestamps": missing_assignment_timestamps,
            "warnings": warnings,
        }
        repos_summary.append(repo_payload)

        totals["reviewers"] += reviewers
        totals["reviewers_with_notifications_enabled"] += reviewers_with_notifications
        totals["reviewers_with_events"] += reviewers_with_events
        totals["reviewers_to_notify"] += reviewers_to_notify
        totals["assigned_items"] += assigned_items
        totals["would_nudge"] += would_nudge
        totals["would_auto_unassign"] += would_auto_unassign
        totals["would_new_assignment_ping"] += would_new_assignment_ping
        totals["missing_assignment_timestamps"] += missing_assignment_timestamps
        totals["warnings"] += warnings

    result: dict[str, Any] = {
        "skipped": False,
        "dry_run": True,
        "reports_enabled": reports_enabled,
        "enforcement_enabled": enforcement_enabled,
        "new_assignment_ping_window_seconds": new_assignment_ping_window_seconds,
        "new_assignment_ping_window_source": new_assignment_ping_window_source,
        "repos": len(repos),
        "repository_id": int(repository_id) if repository_id is not None else None,
        "totals": totals,
        "per_repo": repos_summary,
    }

    log.info(
        "analyzer.reviewer_attention_daily: dry-run summary repos=%s new_assignment=%s nudge=%s auto_unassign=%s notify=%s enforcement=%s",
        result["repos"],
        totals["would_new_assignment_ping"],
        totals["would_nudge"],
        totals["would_auto_unassign"],
        totals["reviewers_to_notify"],
        enforcement_enabled,
    )

    return result
