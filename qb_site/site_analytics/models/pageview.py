from __future__ import annotations

from django.db import models
from django.utils import timezone


class AnalyticsPageView(models.Model):
    """Raw pageview event row.

    Rows are immutable after insert; never updated.
    Raw IP is not stored; privacy-preserving monthly hash is used instead.
    Retained for a bounded window (SITE_ANALYTICS_RETENTION_DAYS) then pruned.
    """

    site = models.CharField(max_length=100)
    path = models.CharField(max_length=2000)
    referrer = models.CharField(max_length=2000, blank=True, default="")
    user_agent = models.CharField(max_length=1000, blank=True, default="")
    occurred_at = models.DateTimeField(default=timezone.now)
    # sha256(ip | normalized_user_agent | salt) — no raw IP stored. The month is not
    # part of the payload: unlinkability across months comes from rotating the salt
    # itself (see SiteAnalyticsSalt and the site_analytics.rotate_salt task).
    visitor_month_hash = models.CharField(max_length=64)

    class Meta:
        indexes = [
            models.Index(fields=["site", "occurred_at"], name="sa_pv_site_occurred_idx"),
            models.Index(fields=["occurred_at"], name="sa_pv_occurred_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.site}:{self.path} @ {self.occurred_at}"
