"""Celery task: rotate the monthly visitor-hash salt."""

from __future__ import annotations

import secrets
from typing import Any

from celery import shared_task
from django.db import transaction

from site_analytics.models.salt import SiteAnalyticsSalt


@shared_task(name="site_analytics.rotate_salt")
def rotate_salt_task() -> dict[str, Any]:
    """Generate a fresh random salt and discard the previous one.

    Runs at the start of each calendar month.  The old salt is deleted so past
    visitor hashes cannot be re-derived even if the new salt is ever leaked
    (forward secrecy).
    """
    new_salt = secrets.token_hex(32)
    with transaction.atomic():
        obj = SiteAnalyticsSalt.objects.create(salt=new_salt)
        deleted, _ = SiteAnalyticsSalt.objects.exclude(pk=obj.pk).delete()
    return {"rotated": True, "old_deleted": deleted}
