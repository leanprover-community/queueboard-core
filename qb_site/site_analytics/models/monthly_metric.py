from __future__ import annotations

from django.db import models


class AnalyticsMonthlyMetric(models.Model):
    """Aggregated monthly pageview and unique-visitor counts per site.

    Rows are upserted by the ``site_analytics.aggregate_monthly_metrics`` task.
    Reporting queries must use this table, not raw ``AnalyticsPageView`` scans.

    ``month`` is stored as the first day of the UTC month (e.g. 2026-03-01)
    so it is a plain ``DateField`` with natural ordering and easy filtering.
    """

    site = models.CharField(max_length=100)
    month = models.DateField()  # UTC first-of-month, e.g. 2026-03-01
    pageviews = models.PositiveIntegerField(default=0)
    unique_visitors = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["site", "month"], name="sa_monthly_site_month_uniq"),
        ]
        indexes = [
            models.Index(fields=["site", "month"], name="sa_monthly_site_month_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.site} {self.month:%Y-%m}: {self.pageviews} pv / {self.unique_visitors} uv"
