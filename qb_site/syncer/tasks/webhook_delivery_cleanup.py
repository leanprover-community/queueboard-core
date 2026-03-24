from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Dict

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from syncer.models import GitHubWebhookDelivery

logger = logging.getLogger(__name__)


@shared_task(name="syncer.expire_old_webhook_deliveries")
def expire_old_webhook_deliveries_task(  # type: ignore[no-redef]
    retention_days: int | None = None,
) -> Dict[str, Any]:
    """Delete GitHubWebhookDelivery rows older than retention_days.

    Rows are matched by received_at.  The default retention window is
    controlled by SYNCER_WEBHOOK_DELIVERY_RETENTION_DAYS (default 7).
    """
    if retention_days is None:
        retention_days = int(getattr(settings, "SYNCER_WEBHOOK_DELIVERY_RETENTION_DAYS", 7))
    retention_days_int = int(retention_days)

    cutoff = timezone.now() - timedelta(days=retention_days_int)
    deleted, _ = GitHubWebhookDelivery.objects.filter(received_at__lt=cutoff).delete()

    logger.info("expire_old_webhook_deliveries: deleted=%d retention_days=%d", deleted, retention_days_int)
    return {
        "retention_days": retention_days_int,
        "cutoff": cutoff.isoformat(),
        "deleted": deleted,
    }
