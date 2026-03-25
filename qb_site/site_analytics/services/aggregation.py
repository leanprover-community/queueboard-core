"""Aggregation services for site analytics."""

from __future__ import annotations

import datetime
from typing import Any

from django.db.models import Count
from django.utils import timezone

from site_analytics.models import AnalyticsDailyMetric, AnalyticsPageView


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
