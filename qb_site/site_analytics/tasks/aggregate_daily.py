"""Celery task: aggregate daily pageview metrics."""

from __future__ import annotations

from typing import Any

from celery import shared_task

from site_analytics.services.aggregation import aggregate_daily_metrics


@shared_task(name="site_analytics.aggregate_daily_metrics")
def aggregate_daily_metrics_task(*, days_back: int = 2) -> dict[str, Any]:
    """Idempotent upsert of daily pageview and unique-visitor counts.

    Recomputes the rolling ``days_back`` UTC calendar days so that events
    arriving near midnight or during a prior task run are never missed.
    Safe to retry: each run overwrites aggregates with a fresh count.
    """
    return aggregate_daily_metrics(days_back=days_back)
