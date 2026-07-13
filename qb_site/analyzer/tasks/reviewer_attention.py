from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, time, timezone
from typing import Any

from celery import shared_task
from django.conf import settings
from django.utils import timezone as dj_timezone

from analyzer.models import (
    ReviewerAttentionAutoUnassignRecord,
    ReviewerAttentionDailyRun,
    ReviewerAttentionNotificationRecord,
)
from analyzer.services import build_reviewer_attention_reports
from analyzer.services.reviewer_attention import ReviewerAttentionItem, ReviewerAttentionReport
from analyzer.services.reviewer_load import ReviewerLoad, build_reviewer_loads, format_load_line, normalize_login
from analyzer.services.reviewer_attention_format import (
    format_since_timestamp,
    render_consecutive_queue_time_since_assignment_line,
    render_total_queue_time_line,
    sort_by_assignment_recency,
    sort_by_queue_age,
)
from core.services.github_assignment import AssignmentMutationError, GitHubAssignmentClient
from core.services.github_operation_tokens import resolve_github_app_operation_token
from core.models import Repository, User
from core.utils.zulip_time import format_global_time
from zulip_bot.services.zulip_client import ZulipApiError, ZulipClient


log = logging.getLogger(__name__)
MAX_MESSAGE_CHARS = 9000


def _iter_item_notification_categories(item: ReviewerAttentionItem) -> list[str]:
    categories: list[str] = []
    if item.needs_new_assignment_ping:
        categories.append(ReviewerAttentionNotificationRecord.CATEGORY_NEW_ASSIGNMENT)
    if item.needs_nudge:
        categories.append(ReviewerAttentionNotificationRecord.CATEGORY_NUDGE)
    if item.needs_auto_unassign:
        categories.append(ReviewerAttentionNotificationRecord.CATEGORY_AUTO_UNASSIGN)
    return categories


def _notification_cycle_anchor(item: ReviewerAttentionItem) -> datetime | None:
    if item.queue_anchor_at is not None:
        return item.queue_anchor_at
    return item.last_assigned_at


def _claim_notification_categories(
    *,
    run: ReviewerAttentionDailyRun,
    run_date,
    reviewer_id: int,
    report: ReviewerAttentionReport,
    now_ts: datetime,
) -> set[tuple[int, int, int, str, datetime]]:
    claimed: set[tuple[int, int, int, str, datetime]] = set()
    for item in report.items:
        pr_number = int(item.pr_number)
        cycle_anchor_at = _notification_cycle_anchor(item)
        if cycle_anchor_at is None:
            continue
        for category in _iter_item_notification_categories(item):
            record, created = ReviewerAttentionNotificationRecord.objects.get_or_create(
                repository_id=int(report.repository_id),
                reviewer_id=reviewer_id,
                pr_number=pr_number,
                category=category,
                cycle_anchor_at=cycle_anchor_at,
                defaults={
                    "run_date": run_date,
                    "status": ReviewerAttentionNotificationRecord.STATUS_PENDING,
                    "run": run,
                },
            )
            if created:
                claimed.add((int(report.repository_id), reviewer_id, pr_number, category, cycle_anchor_at))
                continue
            if record.status == ReviewerAttentionNotificationRecord.STATUS_FAILED:
                record.status = ReviewerAttentionNotificationRecord.STATUS_PENDING
                record.error = ""
                record.delivered_at = None
                record.run = run
                record.save(update_fields=["status", "error", "delivered_at", "run", "updated_at"])
                claimed.add((int(report.repository_id), reviewer_id, pr_number, category, cycle_anchor_at))
                continue
            if record.status == ReviewerAttentionNotificationRecord.STATUS_PENDING:
                # Consider stale pending records claimable to avoid permanent lockouts from crashed runs.
                if (now_ts - record.updated_at).total_seconds() >= 2 * 60 * 60:
                    record.run = run
                    record.save(update_fields=["run", "updated_at"])
                    claimed.add((int(report.repository_id), reviewer_id, pr_number, category, cycle_anchor_at))
                continue
    return claimed


def _filter_report_for_claimed_categories(
    *,
    report: ReviewerAttentionReport,
    reviewer_id: int,
    claimed_keys: set[tuple[int, int, int, str, datetime]],
) -> ReviewerAttentionReport:
    filtered_items: list[ReviewerAttentionItem] = []
    for item in report.items:
        pr_number = int(item.pr_number)
        cycle_anchor_at = _notification_cycle_anchor(item)
        new_assignment = (
            item.needs_new_assignment_ping
            and cycle_anchor_at is not None
            and (
                int(report.repository_id),
                reviewer_id,
                pr_number,
                ReviewerAttentionNotificationRecord.CATEGORY_NEW_ASSIGNMENT,
                cycle_anchor_at,
            )
            in claimed_keys
        )
        needs_nudge = (
            item.needs_nudge
            and cycle_anchor_at is not None
            and (
                int(report.repository_id),
                reviewer_id,
                pr_number,
                ReviewerAttentionNotificationRecord.CATEGORY_NUDGE,
                cycle_anchor_at,
            )
            in claimed_keys
        )
        needs_auto_unassign = (
            item.needs_auto_unassign
            and cycle_anchor_at is not None
            and (
                int(report.repository_id),
                reviewer_id,
                pr_number,
                ReviewerAttentionNotificationRecord.CATEGORY_AUTO_UNASSIGN,
                cycle_anchor_at,
            )
            in claimed_keys
        )
        if not (new_assignment or needs_nudge or needs_auto_unassign):
            continue
        filtered_items.append(
            replace(
                item,
                needs_new_assignment_ping=new_assignment,
                needs_nudge=needs_nudge,
                needs_auto_unassign=needs_auto_unassign,
            )
        )
    return replace(report, items=tuple(filtered_items))


def _mark_notification_records_sent(
    *,
    run: ReviewerAttentionDailyRun,
    claimed_keys: set[tuple[int, int, int, str, datetime]],
    now_ts: datetime,
) -> None:
    for repository_id, reviewer_id, pr_number, category, cycle_anchor_at in claimed_keys:
        ReviewerAttentionNotificationRecord.objects.filter(
            repository_id=repository_id,
            reviewer_id=reviewer_id,
            pr_number=pr_number,
            category=category,
            cycle_anchor_at=cycle_anchor_at,
        ).update(
            status=ReviewerAttentionNotificationRecord.STATUS_SENT,
            delivered_at=now_ts,
            error="",
            run=run,
        )


def _mark_notification_records_failed(
    *,
    run: ReviewerAttentionDailyRun,
    claimed_keys: set[tuple[int, int, int, str, datetime]],
    error: str,
) -> None:
    for repository_id, reviewer_id, pr_number, category, cycle_anchor_at in claimed_keys:
        ReviewerAttentionNotificationRecord.objects.filter(
            repository_id=repository_id,
            reviewer_id=reviewer_id,
            pr_number=pr_number,
            category=category,
            cycle_anchor_at=cycle_anchor_at,
        ).update(
            status=ReviewerAttentionNotificationRecord.STATUS_FAILED,
            delivered_at=None,
            error=error[:2000],
            run=run,
        )


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


def _coerce_utc_datetime(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            return raw.replace(tzinfo=timezone.utc)
        return raw.astimezone(timezone.utc)
    if isinstance(raw, str):
        value = raw.strip()
        if not value:
            return None
        try:
            if "T" not in value and " " not in value:
                parsed_date = datetime.strptime(value, "%Y-%m-%d").date()
                return datetime.combine(parsed_date, time.min, tzinfo=timezone.utc)
            normalized = value.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


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


def _format_consecutive_queue_age_line(item: ReviewerAttentionItem) -> str:
    return f"  - {render_consecutive_queue_time_since_assignment_line(item)}"


def _format_total_queue_time_line(item: ReviewerAttentionItem) -> str:
    return f"  - {render_total_queue_time_line(item)}"


def _render_reviewer_message(
    *,
    reviewer_login: str,
    repo_reports: list[tuple[str, ReviewerAttentionReport]],
    enforcement_enabled: bool,
    unassign_outcomes: dict[tuple[int, int, int], str],
    loads_by_repo_id: dict[int, dict[str, ReviewerLoad]] | None = None,
) -> str:
    loads_by_repo_id = loads_by_repo_id or {}
    reviewer_login_norm = normalize_login(reviewer_login)
    lines: list[str] = [
        "### Assigned queue PRs that may need your attention",
        "",
        f"Generated at {format_global_time(_now_utc_unix())}.",
        "",
    ]

    for repo_label, report in repo_reports:
        new_items = sort_by_assignment_recency([item for item in report.items if item.needs_new_assignment_ping])
        nudge_items = sort_by_queue_age([item for item in report.items if item.needs_nudge])
        unassign_items = sort_by_queue_age([item for item in report.items if item.needs_auto_unassign])
        if not (new_items or nudge_items or unassign_items):
            continue

        lines.append(f"## {repo_label}")
        lines.append(
            "Settings: nudge after "
            f"{report.stale_nudge_days} consecutive days on queue since assignment; "
            f"auto-unassign at {report.auto_unassign_days} days."
        )
        # Load context (this digest never lists the full roster, so include the raw assigned count).
        load = loads_by_repo_id.get(int(report.repository_id), {}).get(reviewer_login_norm)
        if load is not None:
            lines.append(format_load_line(load, include_assigned_count=True))
        lines.append("")
        if new_items:
            lines.append(f"#### Newly assigned ({len(new_items)})")
            lines.append("Assigned recently and worth an initial pass.")
            for item in new_items:
                lines.append(f"{_format_item_line(item)}")
                lines.append(f"  - since {format_since_timestamp(item.last_assigned_at)}")
        if nudge_items:
            lines.append(f"#### Queue attention ({len(nudge_items)})")
            lines.append(f"At least {report.stale_nudge_days} consecutive days on queue since assignment.")
            for item in nudge_items:
                lines.append(f"{_format_item_line(item)}")
                lines.append(_format_consecutive_queue_age_line(item))
                lines.append(_format_total_queue_time_line(item))
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
                    lines.append(f"#### Auto-unassigned in this run ({len(unassigned_items)})")
                    lines.append(f"After at least {report.auto_unassign_days} consecutive days on queue since assignment.")
                    for item in unassigned_items:
                        lines.append(f"{_format_item_line(item)}")
                        lines.append(_format_consecutive_queue_age_line(item))
                        lines.append(_format_total_queue_time_line(item))
                        lines.append(f"  - assigned {format_since_timestamp(item.last_assigned_at)}")
                if threshold_items:
                    lines.append(f"#### At auto-unassign threshold ({len(threshold_items)})")
                    lines.append(f"At least {report.auto_unassign_days} consecutive days on queue since assignment.")
                    for item in threshold_items:
                        lines.append(f"{_format_item_line(item)}")
                        lines.append(_format_consecutive_queue_age_line(item))
                        lines.append(_format_total_queue_time_line(item))
                        lines.append(f"  - assigned {format_since_timestamp(item.last_assigned_at)}")
            else:
                lines.append(f"#### At auto-unassign threshold ({len(unassign_items)})")
                lines.append(f"At least {report.auto_unassign_days} consecutive days on queue since assignment.")
                for item in unassign_items:
                    lines.append(f"{_format_item_line(item)}")
                    lines.append(_format_consecutive_queue_age_line(item))
                    lines.append(_format_total_queue_time_line(item))
                    lines.append(f"  - assigned {format_since_timestamp(item.last_assigned_at)}")
        lines.append("")

    lines.append("Tips:")
    lines.append("- Unassign yourself: `unassign #<number>`")
    lines.append("- See all your assigned PRs: `assigned-prs`")
    lines.append("- Change notification and auto-assignment settings: `prefs`")

    return "\n".join(lines).strip()


@shared_task(name="analyzer.reviewer_attention_daily")
def reviewer_attention_daily_task(
    *,
    repository_id: int | None = None,
    include_inactive_repositories: bool = False,
    reports_enabled_override: bool | None = None,
    enforcement_enabled_override: bool | None = None,
    delivery_enabled_override: bool | None = None,
    delivery_reviewer_user_ids: list[int] | tuple[int, ...] | None = None,
    new_assignment_ping_window_seconds_override: int | None = None,
    policy_start_at_override: datetime | str | None = None,
) -> dict[str, Any]:
    """Run daily reviewer-attention policy sweep and optional summary delivery."""

    reports_enabled = (
        bool(reports_enabled_override)
        if reports_enabled_override is not None
        else bool(getattr(settings, "ANALYZER_REVIEWER_ATTENTION_ENABLED", False))
    )
    enforcement_enabled = (
        bool(enforcement_enabled_override)
        if enforcement_enabled_override is not None
        else bool(getattr(settings, "ANALYZER_REVIEWER_ATTENTION_ENFORCEMENT_ENABLED", False))
    )
    delivery_enabled = (
        bool(delivery_enabled_override)
        if delivery_enabled_override is not None
        else bool(getattr(settings, "ANALYZER_REVIEWER_ATTENTION_DELIVERY_ENABLED", False))
    )
    if new_assignment_ping_window_seconds_override is not None:
        new_assignment_ping_window_seconds = max(1, int(new_assignment_ping_window_seconds_override))
        new_assignment_ping_window_source = "override"
    else:
        new_assignment_ping_window_seconds, new_assignment_ping_window_source = _derive_new_assignment_ping_window_seconds()
    policy_start_at = (
        _coerce_utc_datetime(policy_start_at_override)
        if policy_start_at_override is not None
        else _coerce_utc_datetime(getattr(settings, "ANALYZER_REVIEWER_ATTENTION_POLICY_START_AT", None))
    )

    if not reports_enabled:
        return {
            "skipped": True,
            "reason": "feature_disabled",
            "reports_enabled": reports_enabled,
            "enforcement_enabled": enforcement_enabled,
        }

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
            "reports_enabled": reports_enabled,
            "enforcement_enabled": enforcement_enabled,
        }

    now_ts = dj_timezone.now()
    run_date = now_ts.date()
    run_repository = repos[0] if repository_id is not None and len(repos) == 1 else None
    daily_run = ReviewerAttentionDailyRun.objects.create(
        run_date=run_date,
        started_at=now_ts,
        status="started",
        reports_enabled=reports_enabled,
        enforcement_enabled=enforcement_enabled,
        delivery_enabled=delivery_enabled,
        repository=run_repository,
    )

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
    seen_auto_unassign_candidates: set[tuple[int, int, int]] = set()

    for repo in repos:
        reports = build_reviewer_attention_reports(
            repository=repo,
            new_assignment_ping_window_seconds=new_assignment_ping_window_seconds,
            policy_start_at=policy_start_at,
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
                    candidate_key = (int(repo.id), int(report.reviewer_user_id), int(item.pr_number))
                    if candidate_key in seen_auto_unassign_candidates:
                        continue
                    seen_auto_unassign_candidates.add(candidate_key)
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
        "skipped_already_recorded": 0,
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
            reviewer_user_id = int(candidate["reviewer_user_id"])
            candidate_key = (repo_id, reviewer_user_id, pr_number)
            record, created = ReviewerAttentionAutoUnassignRecord.objects.get_or_create(
                run_date=run_date,
                repository_id=repo_id,
                reviewer_id=reviewer_user_id,
                pr_number=pr_number,
                defaults={
                    "status": ReviewerAttentionAutoUnassignRecord.STATUS_PENDING,
                    "run": daily_run,
                },
            )
            if not created:
                enforcement_stats["skipped_already_recorded"] += 1
                unassign_outcomes[candidate_key] = record.status
                continue
            token = resolve_github_app_operation_token(operation="unassign_pr", owner=owner, repo=repo_name)
            if not token:
                enforcement_stats["skipped_no_token"] += 1
                unassign_outcomes[candidate_key] = ReviewerAttentionAutoUnassignRecord.STATUS_SKIPPED_NO_TOKEN
                record.status = ReviewerAttentionAutoUnassignRecord.STATUS_SKIPPED_NO_TOKEN
                record.completed_at = dj_timezone.now()
                record.error = ""
                record.run = daily_run
                record.save(update_fields=["status", "completed_at", "error", "run", "updated_at"])
                continue
            client = assignment_clients_by_repo.get(repo_id)
            if client is None:
                client = GitHubAssignmentClient(token=token)
                assignment_clients_by_repo[repo_id] = client
            enforcement_stats["attempted"] += 1
            try:
                client.unassign(owner=owner, repo=repo_name, number=pr_number, github_login=reviewer_login)
                enforcement_stats["unassigned"] += 1
                unassign_outcomes[candidate_key] = ReviewerAttentionAutoUnassignRecord.STATUS_UNASSIGNED
                record.status = ReviewerAttentionAutoUnassignRecord.STATUS_UNASSIGNED
                record.completed_at = dj_timezone.now()
                record.error = ""
                record.run = daily_run
                record.save(update_fields=["status", "completed_at", "error", "run", "updated_at"])
            except AssignmentMutationError as exc:
                enforcement_stats["failed"] += 1
                unassign_outcomes[candidate_key] = ReviewerAttentionAutoUnassignRecord.STATUS_FAILED
                record.status = ReviewerAttentionAutoUnassignRecord.STATUS_FAILED
                record.completed_at = dj_timezone.now()
                record.error = str(exc)[:2000]
                record.run = daily_run
                record.save(update_fields=["status", "completed_at", "error", "run", "updated_at"])
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
    delivery_filter_user_ids: set[int] | None = None
    if delivery_reviewer_user_ids is not None:
        delivery_filter_user_ids = {int(user_id) for user_id in delivery_reviewer_user_ids}
    skipped_by_reviewer_filter = 0
    if delivery_filter_user_ids is not None:
        filtered_reports_by_reviewer = {
            int(user_id): payload for user_id, payload in reports_by_reviewer.items() if int(user_id) in delivery_filter_user_ids
        }
        skipped_by_reviewer_filter = len(reports_by_reviewer) - len(filtered_reports_by_reviewer)
        reports_by_reviewer = filtered_reports_by_reviewer

    deliveries: list[dict[str, Any]] = []
    delivery_stats = {
        "attempted": 0,
        "sent": 0,
        "failed": 0,
        "skipped_no_user": 0,
        "skipped_no_zulip_user_id": 0,
        "skipped_delivery_disabled": 0,
        "skipped_by_reviewer_filter": skipped_by_reviewer_filter,
        "skipped_already_sent": 0,
    }

    client: ZulipClient | None = None
    client_init_error: str | None = None
    if delivery_enabled:
        try:
            client = ZulipClient()
        except ZulipApiError as exc:
            client_init_error = str(exc)
            log.warning("analyzer.reviewer_attention_daily: unable to initialize Zulip client: %s", client_init_error)

    # Per-repo reviewer load (weighted, as of the latest queue snapshot), only when we will actually
    # render/send messages. Computed once per repo and shared across reviewers.
    loads_by_repo_id: dict[int, dict[str, ReviewerLoad]] = {}
    if delivery_enabled and client is not None:
        for repo in repos:
            loads_by_repo_id[int(repo.id)] = build_reviewer_loads(repo)

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

        reviewer_claimed_notification_keys: set[tuple[int, int, int, str, datetime]] = set()
        filtered_repo_reports: list[tuple[str, ReviewerAttentionReport]] = []
        for repo_label, report in list(payload["repo_reports"]):
            claimed = _claim_notification_categories(
                run=daily_run,
                run_date=run_date,
                reviewer_id=reviewer_user_id,
                report=report,
                now_ts=now_ts,
            )
            if not claimed:
                continue
            reviewer_claimed_notification_keys.update(claimed)
            filtered_report = _filter_report_for_claimed_categories(
                report=report,
                reviewer_id=reviewer_user_id,
                claimed_keys=reviewer_claimed_notification_keys,
            )
            if filtered_report.has_notifications_to_send:
                filtered_repo_reports.append((repo_label, filtered_report))

        if not filtered_repo_reports or not reviewer_claimed_notification_keys:
            delivery_stats["skipped_already_sent"] += 1
            deliveries.append(
                {
                    "reviewer_user_id": reviewer_user_id,
                    "reviewer_login": reviewer_login,
                    "status": "skipped_already_sent",
                }
            )
            continue

        message = _render_reviewer_message(
            reviewer_login=reviewer_login,
            repo_reports=filtered_repo_reports,
            enforcement_enabled=enforcement_enabled,
            unassign_outcomes=unassign_outcomes,
            loads_by_repo_id=loads_by_repo_id,
        )
        chunks = _split_message_chunks(content=message, max_chars=MAX_MESSAGE_CHARS)
        delivery_stats["attempted"] += 1
        try:
            for chunk in chunks:
                client.send_direct_message(to=[int(user.zulip_user_id)], content=chunk)
            _mark_notification_records_sent(
                run=daily_run,
                claimed_keys=reviewer_claimed_notification_keys,
                now_ts=dj_timezone.now(),
            )
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
            _mark_notification_records_failed(
                run=daily_run,
                claimed_keys=reviewer_claimed_notification_keys,
                error=str(exc),
            )
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
        "include_inactive_repositories": bool(include_inactive_repositories),
        "new_assignment_ping_window_seconds": new_assignment_ping_window_seconds,
        "new_assignment_ping_window_source": new_assignment_ping_window_source,
        "policy_start_at": policy_start_at.isoformat() if policy_start_at is not None else None,
        "repos": len(repos),
        "repository_id": int(repository_id) if repository_id is not None else None,
        "run_id": int(daily_run.id),
        "run_date": str(run_date.isoformat()),
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

    daily_run.status = "completed"
    daily_run.completed_at = dj_timezone.now()
    daily_run.summary = {
        "repos": result["repos"],
        "totals": totals,
        "delivery": delivery_stats,
        "enforcement": enforcement_stats,
    }
    daily_run.errors = []
    daily_run.save(update_fields=["status", "completed_at", "summary", "errors", "updated_at"])

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
