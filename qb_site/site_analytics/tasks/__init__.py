"""Celery tasks for site analytics aggregation and retention."""

from __future__ import annotations

from site_analytics.tasks.aggregate_daily import aggregate_daily_metrics_task  # noqa: F401

__all__ = ["aggregate_daily_metrics_task"]
