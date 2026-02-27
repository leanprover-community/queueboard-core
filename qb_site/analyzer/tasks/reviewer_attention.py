from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from celery import shared_task
from django.conf import settings

from analyzer.services import build_reviewer_attention_reports
from analyzer.services.reviewer_attention_format import format_since_timestamp, sort_by_assignment_recency, sort_by_queue_age
from analyzer.services.reviewer_attention import ReviewerAttentionItem, ReviewerAttentionReport
from core.services.github_assignment import AssignmentMutationError, GitHubAssignmentClient
from core.services.github_operation_tokens import resolve_github_app_operation_token
from core.models import Repository, User
from zulip_bot.services.zulip_client import ZulipApiError, ZulipClient


log = logging.getLogger(__name__)
MAX_MESSAGE_CHARS = 9000


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


def _now_utc_unix() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _split_message_chunks(*, content: str, max_chars: int) -> list[str]:
    if len(content) <= max_chars:
        return [content]

    lines = content.splitlines()
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        line_len = len(line) + 1
        if current and current_len + line_len > max_chars:
            chunks.append("\n".join(current))
            current = [line]
            current_len = line_len
            continue
        if not current and line_len > max_chars:
            start = 0
            while start < len(line):
                end = min(start + max_chars, len(line))
                chunks.append(line[start:end])
                start = end
            continue
        current.append(line)
        current_len += line_len

    if current:
        chunks.append("\n".join(current))
    return chunks


def _format_item_line(item: ReviewerAttentionItem) -> str:
    return f"- PR #{item.pr_number}: {item.pr_title}"


def _render_reviewer_message(
    *,
    reviewer_login: str,
    repo_reports: list[tuple[str, ReviewerAttentionReport]],
    enforcement_enabled: bool,
    unassign_outcomes: dict[tuple[int, int, int], str],
) -> str:
    lines: list[str] = [
        "### Assigned queue PRs that need your attention",
        "",
        f"Generated at <time:{_now_utc_unix()}>.",
        "",
    ]

    for repo_label, report in repo_reports:
        new_items = sort_by_assignment_recency([item for item in report.items if item.needs_new_assignment_ping])
        nudge_items = sort_by_queue_age([item for item in report.items if item.needs_nudge])
        unassign_items = sort_by_queue_age([item for item in report.items if item.needs_auto_unassign])
        if not (new_items or nudge_items or unassign_items):
            continue

        lines.append(f"## {repo_label}")
        if new_items:
            lines.append(f"Newly assigned ({len(new_items)}):")
            for item in new_items:
                lines.append(f"{_format_item_line(item)}")
                lines.append(f"  - since {format_since_timestamp(item.last_assigned_at)}")
        if nudge_items:
            lines.append(f"Queue attention ({len(nudge_items)}):")
            for item in nudge_items:
                lines.append(f"{_format_item_line(item)}")
                lines.append(f"  - on queue for {item.days_on_queue_since_assignment or 0} consecutive days since assignment")
                lines.append(f"  - assigned {format_since_timestamp(item.last_assigned_at)}")
        if unassign_items:
            if enforcement_enabled:
                unassigned_items: list[ReviewerAttentionItem] = []
                threshold_items: list[ReviewerAttentionItem] = []
                for item in unassign_items:
                    outcome = unassign_outcomes.get((report.repository_id, int(report.reviewer_user_id), item.pr_number))
                    if outcome == "unassigned":
                        unassigned_items.append(item)
                    else:
                        threshold_items.append(item)
                if unassigned_items:
                    lines.append(f"Auto-unassigned in this run ({len(unassigned_items)}):")
                    for item in unassigned_items:
                        lines.append(f"{_format_item_line(item)}")
                        lines.append(f"  - queue age at threshold: {item.days_on_queue_since_assignment or 0} consecutive days")
                        lines.append(f"  - assigned {format_since_timestamp(item.last_assigned_at)}")
                if threshold_items:
                    lines.append(f"At auto-unassign threshold ({len(threshold_items)}):")
                    for item in threshold_items:
                        lines.append(f"{_format_item_line(item)}")
                        lines.append(f"  - queue age at threshold: {item.days_on_queue_since_assignment or 0} consecutive days")
                        lines.append(f"  - assigned {format_since_timestamp(item.last_assigned_at)}")
            else:
                lines.append(f"At auto-unassign threshold ({len(unassign_items)}):")
                for item in unassign_items:
                    lines.append(f"{_format_item_line(item)}")
                    lines.append(f"  - queue age at threshold: {item.days_on_queue_since_assignment or 0} consecutive days")
                    lines.append(f"  - assigned {format_since_timestamp(item.last_assigned_at)}")
        lines.append("")

    lines.append("Tips:")
    lines.append("- Unassign yourself: `unassign #<number>`")
    lines.append("- See all your assigned PRs: `assigned_prs`")
    lines.append("- Change notification settings: `prefs`")

    return "\n".join(lines).strip()


@shared_task(name="analyzer.reviewer_attention_daily")
def reviewer_attention_daily_task(*, repository_id: int | None = None) -> dict[str, Any]:
    """Run daily reviewer-attention policy sweep and optional summary delivery."""

    reports_enabled = bool(getattr(settings, "ANALYZER_REVIEWER_ATTENTION_ENABLED", False))
    enforcement_enabled = bool(getattr(settings, "ANALYZER_REVIEWER_ATTENTION_ENFORCEMENT_ENABLED", False))
    delivery_enabled = bool(getattr(settings, "ANALYZER_REVIEWER_ATTENTION_DELIVERY_ENABLED", False))
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
    reports_by_reviewer: dict[int, dict[str, Any]] = {}
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
    auto_unassign_candidates: list[dict[str, Any]] = []

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

        for report in reports:
            for item in report.items:
                if item.needs_auto_unassign:
                    auto_unassign_candidates.append(
                        {
                            "repository_id": int(repo.id),
                            "owner": repo.owner,
                            "repo": repo.name,
                            "reviewer_login": report.reviewer_login,
                            "reviewer_user_id": int(report.reviewer_user_id),
                            "pr_number": int(item.pr_number),
                        }
                    )
            if not report.has_notifications_to_send:
                continue
            bucket = reports_by_reviewer.setdefault(
                int(report.reviewer_user_id),
                {
                    "reviewer_login": report.reviewer_login,
                    "repo_reports": [],
                },
            )
            bucket["repo_reports"].append((repo_payload["repo"], report))

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

    enforcement_stats = {
        "candidates": len(auto_unassign_candidates),
        "attempted": 0,
        "unassigned": 0,
        "failed": 0,
        "skipped_disabled": 0,
        "skipped_no_token": 0,
    }
    unassign_outcomes: dict[tuple[int, int, int], str] = {}
    if enforcement_enabled:
        assignment_clients_by_repo: dict[int, GitHubAssignmentClient] = {}
        for candidate in auto_unassign_candidates:
            repo_id = int(candidate["repository_id"])
            pr_number = int(candidate["pr_number"])
            reviewer_login = str(candidate["reviewer_login"])
            owner = str(candidate["owner"])
            repo_name = str(candidate["repo"])
            token = resolve_github_app_operation_token(operation="unassign_pr", owner=owner, repo=repo_name)
            if not token:
                enforcement_stats["skipped_no_token"] += 1
                unassign_outcomes[(repo_id, int(candidate["reviewer_user_id"]), pr_number)] = "skipped_no_token"
                continue
            client = assignment_clients_by_repo.get(repo_id)
            if client is None:
                client = GitHubAssignmentClient(token=token)
                assignment_clients_by_repo[repo_id] = client
            enforcement_stats["attempted"] += 1
            try:
                client.unassign(owner=owner, repo=repo_name, number=pr_number, github_login=reviewer_login)
                enforcement_stats["unassigned"] += 1
                unassign_outcomes[(repo_id, int(candidate["reviewer_user_id"]), pr_number)] = "unassigned"
            except AssignmentMutationError:
                enforcement_stats["failed"] += 1
                unassign_outcomes[(repo_id, int(candidate["reviewer_user_id"]), pr_number)] = "failed"
    else:
        enforcement_stats["skipped_disabled"] = len(auto_unassign_candidates)
        for candidate in auto_unassign_candidates:
            unassign_outcomes[
                (int(candidate["repository_id"]), int(candidate["reviewer_user_id"]), int(candidate["pr_number"]))
            ] = "skipped_disabled"

    users_by_id = {
        int(user.id): user
        for user in User.objects.filter(id__in=list(reports_by_reviewer.keys())).only("id", "github_login", "zulip_user_id")
    }
    deliveries: list[dict[str, Any]] = []
    delivery_stats = {
        "attempted": 0,
        "sent": 0,
        "failed": 0,
        "skipped_no_user": 0,
        "skipped_no_zulip_user_id": 0,
        "skipped_delivery_disabled": 0,
    }

    client: ZulipClient | None = None
    client_init_error: str | None = None
    if delivery_enabled:
        try:
            client = ZulipClient()
        except ZulipApiError as exc:
            client_init_error = str(exc)
            log.warning("analyzer.reviewer_attention_daily: unable to initialize Zulip client: %s", client_init_error)

    for reviewer_user_id, payload in sorted(reports_by_reviewer.items()):
        reviewer_login = str(payload["reviewer_login"])
        user = users_by_id.get(reviewer_user_id)
        if user is None:
            delivery_stats["skipped_no_user"] += 1
            deliveries.append(
                {
                    "reviewer_user_id": reviewer_user_id,
                    "reviewer_login": reviewer_login,
                    "status": "skipped_no_user",
                }
            )
            continue
        if not delivery_enabled:
            delivery_stats["skipped_delivery_disabled"] += 1
            deliveries.append(
                {
                    "reviewer_user_id": reviewer_user_id,
                    "reviewer_login": reviewer_login,
                    "status": "skipped_delivery_disabled",
                }
            )
            continue
        if client is None:
            delivery_stats["failed"] += 1
            deliveries.append(
                {
                    "reviewer_user_id": reviewer_user_id,
                    "reviewer_login": reviewer_login,
                    "status": "failed_client_init",
                    "error": client_init_error,
                }
            )
            continue
        if user.zulip_user_id is None:
            delivery_stats["skipped_no_zulip_user_id"] += 1
            deliveries.append(
                {
                    "reviewer_user_id": reviewer_user_id,
                    "reviewer_login": reviewer_login,
                    "status": "skipped_no_zulip_user_id",
                }
            )
            continue

        message = _render_reviewer_message(
            reviewer_login=reviewer_login,
            repo_reports=list(payload["repo_reports"]),
            enforcement_enabled=enforcement_enabled,
            unassign_outcomes=unassign_outcomes,
        )
        chunks = _split_message_chunks(content=message, max_chars=MAX_MESSAGE_CHARS)
        delivery_stats["attempted"] += 1
        try:
            for chunk in chunks:
                client.send_direct_message(to=[int(user.zulip_user_id)], content=chunk)
            delivery_stats["sent"] += 1
            deliveries.append(
                {
                    "reviewer_user_id": reviewer_user_id,
                    "reviewer_login": reviewer_login,
                    "status": "sent",
                    "chunks": len(chunks),
                }
            )
        except ZulipApiError as exc:
            delivery_stats["failed"] += 1
            deliveries.append(
                {
                    "reviewer_user_id": reviewer_user_id,
                    "reviewer_login": reviewer_login,
                    "status": "failed_send",
                    "error": str(exc),
                }
            )

    result: dict[str, Any] = {
        "skipped": False,
        "dry_run": not delivery_enabled,
        "reports_enabled": reports_enabled,
        "enforcement_enabled": enforcement_enabled,
        "delivery_enabled": delivery_enabled,
        "new_assignment_ping_window_seconds": new_assignment_ping_window_seconds,
        "new_assignment_ping_window_source": new_assignment_ping_window_source,
        "repos": len(repos),
        "repository_id": int(repository_id) if repository_id is not None else None,
        "totals": totals,
        "per_repo": repos_summary,
        "delivery": {
            "stats": delivery_stats,
            "per_reviewer": deliveries,
        },
        "enforcement": {
            "stats": enforcement_stats,
        },
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
