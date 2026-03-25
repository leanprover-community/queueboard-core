"""Aggregation and retention services for site analytics."""

from __future__ import annotations

import datetime
from typing import Any

from django.conf import settings
from django.db.models import Count
from django.utils import timezone

from site_analytics.models import AnalyticsDailyMetric, AnalyticsMonthlyMetric, AnalyticsPageView


def aggregate_daily_metrics(
    *,
    date: datetime.date | None = None,
    days_back: int = 2,
) -> dict[str, Any]:
    """Idempotent upsert of AnalyticsDailyMetric for a rolling date window.

    By default recomputes today and yesterday (``days_back=2``) so that events
    arriving near midnight or during a prior task run are not missed.  The task
    is safe to retry: each call overwrites the aggregate with a fresh count from
    raw rows, so running it multiple times on the same window is harmless.

    Returns a summary dict suitable for Celery task result storage.
    """
    if date is None:
        date = timezone.now().date()

    target_dates = [date - datetime.timedelta(days=i) for i in range(days_back)]
    upserted = 0
    skipped = 0

    for d in target_dates:
        # COUNT(*) and COUNT(DISTINCT visitor_month_hash) per site for this UTC date.
        # Django's __date lookup respects USE_TZ and the configured TIME_ZONE (UTC),
        # so the date boundary is always UTC midnight.
        rows = (
            AnalyticsPageView.objects.filter(occurred_at__date=d)
            .values("site")
            .annotate(
                pageviews=Count("id"),
                unique_visitors=Count("visitor_month_hash", distinct=True),
            )
        )

        for row in rows:
            AnalyticsDailyMetric.objects.update_or_create(
                site=row["site"],
                date=d,
                defaults={
                    "pageviews": row["pageviews"],
                    "unique_visitors": row["unique_visitors"],
                },
            )
            upserted += 1

        # If no raw rows exist for this date and site, we leave any existing
        # aggregate row in place rather than zeroing it out.  This avoids
        # accidental data loss if the raw rows were pruned before the aggregate
        # was read.
        sites_with_existing = set(AnalyticsDailyMetric.objects.filter(date=d).values_list("site", flat=True))
        sites_in_raw = {r["site"] for r in rows}
        skipped += len(sites_with_existing - sites_in_raw)

    return {
        "dates_processed": [str(d) for d in target_dates],
        "upserted": upserted,
        "skipped_existing_no_raw": skipped,
    }


def _month_start(date: datetime.date) -> datetime.date:
    """Return the first day of the month containing ``date``."""
    return date.replace(day=1)


def aggregate_monthly_metrics(
    *,
    date: datetime.date | None = None,
    months_back: int = 2,
) -> dict[str, Any]:
    """Idempotent upsert of AnalyticsMonthlyMetric for a rolling month window.

    By default recomputes the current month and the previous month
    (``months_back=2``) so that late-arriving events and month-boundary races
    are always captured.  Safe to retry.

    ``month`` values are stored as the first day of the UTC month so they sort
    and filter naturally as ``DateField`` values.
    """
    if date is None:
        date = timezone.now().date()

    # Build the list of first-of-month dates to recompute.
    target_months: list[datetime.date] = []
    current = _month_start(date)
    for _ in range(months_back):
        target_months.append(current)
        # Step back one month: subtract enough days to land in the previous month
        # then take the first of that month.
        current = _month_start(current - datetime.timedelta(days=1))

    upserted = 0
    skipped = 0

    for month_start in target_months:
        # Last day of the month: go to first of next month, subtract one day.
        if month_start.month == 12:
            month_end = datetime.date(month_start.year + 1, 1, 1) - datetime.timedelta(days=1)
        else:
            month_end = datetime.date(month_start.year, month_start.month + 1, 1) - datetime.timedelta(days=1)

        rows = (
            AnalyticsPageView.objects.filter(occurred_at__date__gte=month_start, occurred_at__date__lte=month_end)
            .values("site")
            .annotate(
                pageviews=Count("id"),
                unique_visitors=Count("visitor_month_hash", distinct=True),
            )
        )

        for row in rows:
            AnalyticsMonthlyMetric.objects.update_or_create(
                site=row["site"],
                month=month_start,
                defaults={
                    "pageviews": row["pageviews"],
                    "unique_visitors": row["unique_visitors"],
                },
            )
            upserted += 1

        # Preserve existing rows where raw data has been pruned (same logic as daily).
        sites_with_existing = set(AnalyticsMonthlyMetric.objects.filter(month=month_start).values_list("site", flat=True))
        sites_in_raw = {r["site"] for r in rows}
        skipped += len(sites_with_existing - sites_in_raw)

    return {
        "months_processed": [str(m) for m in target_months],
        "upserted": upserted,
        "skipped_existing_no_raw": skipped,
    }


def prune_old_pageviews(
    *,
    retention_days: int | None = None,
) -> dict[str, Any]:
    """Delete AnalyticsPageView rows older than the retention window.

    ``retention_days`` defaults to ``SITE_ANALYTICS_RETENTION_DAYS`` from
    settings.  Rows whose ``occurred_at`` is strictly before the cutoff are
    deleted in a single query; Postgres will handle the index scan efficiently
    given the index on ``occurred_at``.
    """
    if retention_days is None:
        retention_days = settings.SITE_ANALYTICS_RETENTION_DAYS

    cutoff = timezone.now() - datetime.timedelta(days=retention_days)
    deleted, _ = AnalyticsPageView.objects.filter(occurred_at__lt=cutoff).delete()

    return {
        "deleted": deleted,
        "cutoff": cutoff.isoformat(),
        "retention_days": retention_days,
    }
