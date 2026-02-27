from __future__ import annotations

from datetime import datetime, timedelta, timezone

from analyzer.services.reviewer_attention import ReviewerAttentionItem

CONSECUTIVE_QUEUE_TIME_SINCE_ASSIGNMENT_LABEL = "Consecutive time on queue since latest assignment"
TOTAL_QUEUE_TIME_LABEL = "Total queue time"


def format_compact_duration(total_seconds: int) -> str:
    if total_seconds < 0:
        total_seconds = 0
    delta = timedelta(seconds=int(total_seconds))
    seconds = int(delta.total_seconds())
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)

    if days >= 5:
        return f"{days}d"
    if days >= 1:
        return f"{days}d {hours}h"
    if hours >= 1:
        return f"{hours}h {minutes}m"
    if minutes >= 1:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def format_since_timestamp(ts: datetime | None, *, now: datetime | None = None) -> str:
    if ts is None:
        return "unavailable"
    now_ts = now or datetime.now(timezone.utc)
    if now_ts.tzinfo is None:
        now_ts = now_ts.replace(tzinfo=timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    total_seconds = max(int((now_ts - ts).total_seconds()), 0)
    return f"<time:{int(ts.timestamp())}> ({format_compact_duration(total_seconds)} ago)"


def sort_by_assignment_recency(items: list[ReviewerAttentionItem]) -> list[ReviewerAttentionItem]:
    return sorted(
        items,
        key=lambda item: (
            -(int(item.last_assigned_at.timestamp()) if item.last_assigned_at is not None else -1),
            item.pr_number,
        ),
    )


def sort_by_queue_age(items: list[ReviewerAttentionItem]) -> list[ReviewerAttentionItem]:
    return sorted(
        items,
        key=lambda item: (
            -(item.days_on_queue_since_assignment if item.days_on_queue_since_assignment is not None else -1),
            item.pr_number,
        ),
    )


def format_consecutive_queue_age_since_assignment(item: ReviewerAttentionItem) -> str:
    if item.days_on_queue_since_assignment is None:
        return "unavailable"
    consecutive_seconds = int(item.days_on_queue_since_assignment) * 24 * 60 * 60
    return format_compact_duration(consecutive_seconds)


def format_total_queue_time(item: ReviewerAttentionItem) -> str:
    if item.total_queue_days is None:
        return "unavailable"
    return format_compact_duration(item.total_queue_seconds or 0)


def render_consecutive_queue_time_since_assignment_line(item: ReviewerAttentionItem, *, indent: str = "") -> str:
    return f"{indent}{CONSECUTIVE_QUEUE_TIME_SINCE_ASSIGNMENT_LABEL}: {format_consecutive_queue_age_since_assignment(item)}"


def render_total_queue_time_line(item: ReviewerAttentionItem, *, indent: str = "") -> str:
    return f"{indent}{TOTAL_QUEUE_TIME_LABEL}: {format_total_queue_time(item)}"
