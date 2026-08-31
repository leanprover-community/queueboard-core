"""Celery tasks for site analytics aggregation and retention."""

from __future__ import annotations

from site_analytics.tasks.aggregate_daily import aggregate_daily_metrics_task  # noqa: F401
from site_analytics.tasks.aggregate_monthly import (  # noqa: F401
    aggregate_monthly_metrics_task,
    prune_old_pageviews_task,
)
from site_analytics.tasks.rotate_salt import rotate_salt_task  # noqa: F401

__all__ = [
    "aggregate_daily_metrics_task",
    "aggregate_monthly_metrics_task",
    "prune_old_pageviews_task",
    "rotate_salt_task",
]
