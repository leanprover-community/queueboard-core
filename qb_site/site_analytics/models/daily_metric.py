from __future__ import annotations

from django.db import models


class AnalyticsDailyMetric(models.Model):
    """Aggregated daily pageview and unique-visitor counts per site.

    Rows are upserted by the ``site_analytics.aggregate_daily_metrics`` task;
    never written by the ingestion endpoint.
    Reporting queries must use this table, not raw ``AnalyticsPageView`` scans.
    """

    site = models.CharField(max_length=100)
    date = models.DateField()  # UTC calendar date
    pageviews = models.PositiveIntegerField(default=0)
    unique_visitors = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["site", "date"], name="sa_dailymetric_site_date_unique"),
        ]
        indexes = [
            models.Index(fields=["site", "date"], name="sa_dailymetric_site_date_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.site} {self.date}: {self.pageviews} pv / {self.unique_visitors} uv"
