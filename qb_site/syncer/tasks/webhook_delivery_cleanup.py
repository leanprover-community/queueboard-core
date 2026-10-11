from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Dict

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from syncer.models import GitHubWebhookDelivery

logger = logging.getLogger(__name__)

_DELETE_BATCH_SIZE = 5000


@shared_task(name="syncer.expire_old_webhook_deliveries")
def expire_old_webhook_deliveries_task(  # type: ignore[no-redef]
    retention_days: int | None = None,
    max_rows: int | None = None,
) -> Dict[str, Any]:
    """Delete GitHubWebhookDelivery rows older than retention_days.

    Rows are matched by received_at.  The default retention window is
    controlled by SYNCER_WEBHOOK_DELIVERY_RETENTION_DAYS (default 7).

    Deletes oldest-first in id-cursor batches, each its own short statement,
    so a large backlog never becomes one long transaction. A run stops after
    max_rows rows (SYNCER_WEBHOOK_DELIVERY_CLEANUP_MAX_ROWS; <= 0 means no
    cap) and leaves the rest for the next run; ``capped`` says it stopped there with rows left.
    """
    if retention_days is None:
        retention_days = int(getattr(settings, "SYNCER_WEBHOOK_DELIVERY_RETENTION_DAYS", 7))
    retention_days_int = int(retention_days)
    if max_rows is None:
        max_rows = int(getattr(settings, "SYNCER_WEBHOOK_DELIVERY_CLEANUP_MAX_ROWS", 1_000_000))
    max_rows_int = int(max_rows)

    cutoff = timezone.now() - timedelta(days=retention_days_int)
    qs = GitHubWebhookDelivery.objects.filter(received_at__lt=cutoff)
    deleted = 0
    capped = False
    last_id = 0
    while True:
        if 0 < max_rows_int <= deleted:
            # Stopped at the cap: capped only if matching rows are left for the next run.
            capped = qs.filter(id__gt=last_id).exists()
            break
        limit = _DELETE_BATCH_SIZE if max_rows_int <= 0 else min(_DELETE_BATCH_SIZE, max_rows_int - deleted)
        batch_ids = list(qs.filter(id__gt=last_id).order_by("id").values_list("id", flat=True)[:limit])
        if batch_ids:
            batch_deleted, _ = GitHubWebhookDelivery.objects.filter(id__in=batch_ids).delete()
            deleted += batch_deleted
            last_id = batch_ids[-1]
        if len(batch_ids) < limit:
            break

    logger.info(
        "expire_old_webhook_deliveries: deleted=%d retention_days=%d capped=%s",
        deleted,
        retention_days_int,
        capped,
    )
    return {
        "retention_days": retention_days_int,
        "cutoff": cutoff.isoformat(),
        "deleted": deleted,
        "max_rows": max_rows_int,
        "capped": capped,
    }
