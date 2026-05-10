"""Per-item Celery task for the archive backfill importer (design doc 043).

Each ArchiveImportItem produced by ``bootstrap_archive_worklist`` becomes
one ``import_archive_pr_item`` invocation. The task atomically claims the
row (so two beat ticks can't double-pick), HTTP GETs the legacy payload
from raw.githubusercontent.com, parses the JSON, and hands the unwrapped
``pullRequest`` node to the archive_import service. Result status maps:

- 200 + ingest succeeds → ``completed``.
- 404 → ``failed_permanent`` (the path genuinely doesn't exist in the
  archive). No retry.
- 5xx / network / timeout → ``failed_transient``; the next scheduler tick
  re-picks. Capped at ``ARCHIVE_IMPORT_MAX_TRANSIENT_ATTEMPTS`` after
  which the row flips to ``failed_permanent``.
- JSON parse / archive payload schema error → ``failed_permanent`` with
  the error stored in ``last_error`` for inspection.
- Per-item DB errors → ``failed_transient`` (next tick retries).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

import requests
from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from syncer.models import ArchiveImportItem, ArchiveImportItemStatus
from syncer.services.archive_import import (
    ArchivePayloadError,
    fetch_pr_info,
    import_pr_info_payload,
    unwrap_pr_info_payload,
)

log = logging.getLogger(__name__)


@shared_task(name="syncer.archive_import_pr_item", bind=True)
def import_archive_pr_item(self, item_id: int) -> Dict[str, Any]:
    """Process one ArchiveImportItem end-to-end."""
    if not _claim_item(item_id):
        return {"item_id": item_id, "status": "skipped", "reason": "not_pending_or_already_claimed"}

    item = ArchiveImportItem.objects.select_related("repository").get(pk=item_id)

    try:
        raw_bytes = fetch_pr_info(item.archive_name, item.pr_number)
    except requests.HTTPError as exc:
        return _handle_http_error(item, exc)
    except (requests.ConnectionError, requests.Timeout) as exc:
        return _mark_transient(item, f"network_error: {exc.__class__.__name__}: {exc}")
    except Exception as exc:
        log.exception("archive_import: unexpected fetch error item_id=%s", item_id)
        return _mark_transient(item, f"unexpected_fetch_error: {exc.__class__.__name__}: {exc}")

    try:
        data = json.loads(raw_bytes)
    except json.JSONDecodeError as exc:
        return _mark_permanent(item, f"json_decode_error: {exc}")

    try:
        payload = unwrap_pr_info_payload(data)
    except ArchivePayloadError as exc:
        return _mark_permanent(item, f"payload_shape_error: {exc}")

    try:
        with transaction.atomic():
            res = import_pr_info_payload(
                repository=item.repository,
                payload=payload,
                archive_name=item.archive_name,
                archive_timestamp=item.archive_timestamp,
            )
            now = timezone.now()
            ArchiveImportItem.objects.filter(pk=item.pk).update(
                status=ArchiveImportItemStatus.COMPLETED,
                completed_at=now,
                last_error="",
            )
    except ArchivePayloadError as exc:
        return _mark_permanent(item, f"payload_shape_error: {exc}")
    except Exception as exc:
        log.exception("archive_import: ingest error item_id=%s pr=%s/%s", item_id, item.archive_name, item.pr_number)
        return _mark_transient(item, f"ingest_error: {exc.__class__.__name__}: {exc}")

    return {
        "item_id": item.pk,
        "status": ArchiveImportItemStatus.COMPLETED.value,
        "pr_id": res.pr.pk if res.pr else None,
        "pr_created": res.pr_created,
        "timeline_created": res.timeline_created,
        "timeline_updated": res.timeline_updated,
        "check_runs_created": res.check_runs_created,
        "check_runs_updated": res.check_runs_updated,
        "status_contexts_created": res.status_contexts_created,
        "status_contexts_updated": res.status_contexts_updated,
        "labels_attached": res.labels_attached,
    }


def _claim_item(item_id: int) -> bool:
    """Atomically transition status pending → in_progress.

    Returns True if this call won the claim, False otherwise. The
    UPDATE-WHERE-status='pending' guards against double-pickup if two
    beat ticks ever race to enqueue the same item.
    """
    now = timezone.now()
    rows = ArchiveImportItem.objects.filter(pk=item_id, status=ArchiveImportItemStatus.PENDING).update(
        status=ArchiveImportItemStatus.IN_PROGRESS,
        last_attempted_at=now,
    )
    if rows:
        return True
    # Allow re-entry from failed_transient too (the scheduler picks both).
    rows = ArchiveImportItem.objects.filter(pk=item_id, status=ArchiveImportItemStatus.FAILED_TRANSIENT).update(
        status=ArchiveImportItemStatus.IN_PROGRESS,
        last_attempted_at=now,
    )
    return bool(rows)


def _handle_http_error(item: ArchiveImportItem, exc: requests.HTTPError) -> Dict[str, Any]:
    status = exc.response.status_code if exc.response is not None else None
    if status == 404:
        return _mark_permanent(item, f"http_404: {exc}")
    if status is not None and 500 <= status < 600:
        return _mark_transient(item, f"http_{status}: {exc}")
    # 4xx other than 404: treat as permanent — the path is well-formed but
    # rejected (e.g. rate-limited 403 with a Retry-After we'd ignore here,
    # or auth issues). Operators can retry by manually re-pending the row.
    return _mark_permanent(item, f"http_{status}: {exc}")


def _mark_transient(item: ArchiveImportItem, message: str) -> Dict[str, Any]:
    max_attempts = int(getattr(settings, "ARCHIVE_IMPORT_MAX_TRANSIENT_ATTEMPTS", 5))
    new_attempts = item.attempts + 1
    if new_attempts >= max_attempts:
        ArchiveImportItem.objects.filter(pk=item.pk).update(
            status=ArchiveImportItemStatus.FAILED_PERMANENT,
            attempts=new_attempts,
            last_error=f"{message} (exceeded ARCHIVE_IMPORT_MAX_TRANSIENT_ATTEMPTS={max_attempts})",
        )
        return {"item_id": item.pk, "status": ArchiveImportItemStatus.FAILED_PERMANENT.value, "reason": message}
    ArchiveImportItem.objects.filter(pk=item.pk).update(
        status=ArchiveImportItemStatus.FAILED_TRANSIENT,
        attempts=new_attempts,
        last_error=message,
    )
    return {
        "item_id": item.pk,
        "status": ArchiveImportItemStatus.FAILED_TRANSIENT.value,
        "attempts": new_attempts,
        "reason": message,
    }


def _mark_permanent(item: ArchiveImportItem, message: str) -> Dict[str, Any]:
    ArchiveImportItem.objects.filter(pk=item.pk).update(
        status=ArchiveImportItemStatus.FAILED_PERMANENT,
        attempts=item.attempts + 1,
        last_error=message,
    )
    return {"item_id": item.pk, "status": ArchiveImportItemStatus.FAILED_PERMANENT.value, "reason": message}
