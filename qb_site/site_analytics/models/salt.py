"""Monthly rotating salt for visitor hashing."""

from __future__ import annotations

from django.db import models


class SiteAnalyticsSalt(models.Model):
    """Holds the current month's salt used to compute visitor_month_hash.

    Only one row is live at a time.  The ``rotate_salt`` Celery task creates a
    new row at the start of each month and deletes the previous one.  The old
    salt is intentionally discarded so past hashes cannot be re-derived even if
    the current salt is ever leaked.
    """

    salt = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "site_analytics"
