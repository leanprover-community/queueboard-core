"""Celery tasks: monthly aggregate and raw pageview pruning."""

from __future__ import annotations

from typing import Any

from celery import shared_task

from site_analytics.services.aggregation import aggregate_monthly_metrics, prune_old_pageviews


@shared_task(name="site_analytics.aggregate_monthly_metrics")
def aggregate_monthly_metrics_task(*, months_back: int = 2) -> dict[str, Any]:
    """Idempotent upsert of monthly pageview and unique-visitor counts.

    Recomputes the current month and the previous ``months_back - 1`` months
    so late-arriving events and month-boundary races are always captured.
    Safe to retry.
    """
    return aggregate_monthly_metrics(months_back=months_back)


@shared_task(name="site_analytics.prune_old_pageviews")
def prune_old_pageviews_task(*, retention_days: int | None = None) -> dict[str, Any]:
    """Delete raw AnalyticsPageView rows older than the retention window.

    Uses ``SITE_ANALYTICS_RETENTION_DAYS`` from settings when ``retention_days``
    is not provided.  Aggregate rows (daily/monthly) are never pruned by this task.
    """
    return prune_old_pageviews(retention_days=retention_days)
