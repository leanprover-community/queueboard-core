"""Celery tasks for the archive backfill importer (design doc 043).

Two tasks:
  - ``import_archive_pr_item(item_id)`` — per-item ingest. Each
    ArchiveImportItem produced by ``bootstrap_archive_worklist`` becomes
    one invocation. Claims the row atomically, HTTP GETs the legacy
    ``pr_info.json`` from raw.githubusercontent.com, hands the unwrapped
    ``pullRequest`` node to the archive_import service. Status map:
      - 200 + ingest succeeds → ``completed``.
      - 404 → ``failed_permanent`` (path genuinely absent). No retry.
      - 5xx / network / timeout → ``failed_transient``; next tick re-picks.
        Capped at ``ARCHIVE_IMPORT_MAX_TRANSIENT_ATTEMPTS``, after which
        the row flips to ``failed_permanent``.
      - JSON parse / payload-shape errors → ``failed_permanent``.
      - Other DB errors → ``failed_transient`` (next tick retries).

  - ``archive_import_tick()`` — beat-driven scheduler. Picks up to
    ``ARCHIVE_IMPORT_BATCH_SIZE`` rows where status is ``pending`` or
    ``failed_transient`` (oldest ``last_attempted_at`` first, NULL first
    so brand-new pending rows go ahead of already-retried transient
    ones), enqueues each as ``import_archive_pr_item.delay(item_id)``.
    Honors ``ARCHIVE_IMPORT_ENABLED`` inside the task — operators flip
    the env var to enable/disable activity without restarting beat.

  - ``resync_archive_touched_tick()`` — beat-driven drain for the
    forced-resync remediation (doc 043 follow-up). Each tick enqueues up
    to ``ARCHIVE_RESYNC_PER_TICK`` ``sync_pr(force=True)`` tasks from
    ``archive_touched_resync_targets`` (open first, stalest sync first).
    Disabled by default (``ARCHIVE_RESYNC_PER_TICK=0``); skips a tick
    when the cached GitHub rate budget is below
    ``ARCHIVE_RESYNC_MIN_RATE_REMAINING`` so the live pipeline keeps
    headroom. Self-completing: healed PRs drop out of the target set, so
    once the set is empty the tick is a cheap no-op.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

import requests
from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from core.models import Repository
from syncer.models import ArchiveImportItem, ArchiveImportItemStatus
from syncer.services.archive_import import (
    ArchivePayloadError,
    archive_touched_resync_targets,
    fetch_pr_info,
    import_pr_info_payload,
    unwrap_pr_info_payload,
)
from syncer.services.github_client import GitHubClient
from syncer.services.rate_budget import get_rate_snapshot
from syncer.services.task_dedupe import claim_enqueue_slot, sync_pr_enqueue_key
from syncer.tasks.sync_tasks import sync_pr_task

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


@shared_task(name="syncer.archive_import_tick", bind=True)
def archive_import_tick(self) -> Dict[str, Any]:
    """Periodically fan out pending archive worklist items to the per-item task.

    Beat fires this unconditionally on its cadence; the
    ``ARCHIVE_IMPORT_ENABLED`` gate inside the task lets operators
    toggle activity via env var without restarting beat.

    Selection: at most ``ARCHIVE_IMPORT_BATCH_SIZE`` rows where status is
    pending or failed_transient, ordered by ``last_attempted_at NULLS
    FIRST``. ``in_progress`` is intentionally excluded — those have a
    live worker; the scheduler must not double-pick.
    """
    if not bool(getattr(settings, "ARCHIVE_IMPORT_ENABLED", False)):
        return {"status": "disabled", "enqueued": 0}

    batch_size = max(1, int(getattr(settings, "ARCHIVE_IMPORT_BATCH_SIZE", 10)))
    pickable = (
        ArchiveImportItem.objects.filter(
            status__in=[
                ArchiveImportItemStatus.PENDING,
                ArchiveImportItemStatus.FAILED_TRANSIENT,
            ]
        )
        .order_by(F("last_attempted_at").asc(nulls_first=True), "id")
        .values_list("pk", flat=True)[:batch_size]
    )
    item_ids = list(pickable)
    enqueued: list[tuple[int, str]] = []
    for item_id in item_ids:
        async_result = import_archive_pr_item.delay(item_id)
        enqueued.append((item_id, getattr(async_result, "id", "")))
    return {"status": "ok", "enqueued": len(enqueued), "items": enqueued}


@shared_task(name="syncer.resync_archive_touched_tick", bind=True)
def resync_archive_touched_tick(self) -> Dict[str, Any]:
    """Drip-feed forced resyncs of archive-touched live PRs (doc 043 follow-up).

    Beat fires this unconditionally on its cadence; ``ARCHIVE_RESYNC_PER_TICK``
    (0 = disabled, the default) gates activity inside the task so operators
    enable/disable the drain via env var. Each active tick:

    - skips entirely when the cached rate snapshot for the repo's sync token
      reports fewer than ``ARCHIVE_RESYNC_MIN_RATE_REMAINING`` points, so the
      drain never eats the live pipeline's GraphQL headroom (no snapshot /
      no token → fail open; ``sync_pr`` has its own low-budget deferral),
    - takes the next ``ARCHIVE_RESYNC_PER_TICK`` targets from
      ``archive_touched_resync_targets`` (open first, stalest sync first),
    - claims the standard sync_pr enqueue-dedupe slot per PR so a still-queued
      or budget-deferred task from a previous tick is not enqueued twice,
    - enqueues ``sync_pr(force=True)`` for each claimed target.

    A successful forced sync advances ``last_synced_at``, dropping the PR out
    of the healed-excluded target set — the drain converges and the tick
    becomes a cheap no-op (``status=drained``) once remediation is complete.
    """
    per_tick = int(getattr(settings, "ARCHIVE_RESYNC_PER_TICK", 0))
    if per_tick <= 0:
        return {"status": "disabled", "enqueued": 0}

    rate_floor = int(getattr(settings, "ARCHIVE_RESYNC_MIN_RATE_REMAINING", 2500))
    dedupe_ttl = int(getattr(settings, "SYNCER_SYNC_PR_DEDUPE_TTL_SECONDS", 300))

    targets = archive_touched_resync_targets()
    remaining = targets.count()
    if remaining == 0:
        return {"status": "drained", "enqueued": 0, "remaining": 0}

    # Over-fetch so dedupe-skipped rows do not shrink the effective chunk.
    rows = list(targets.values_list("repository_id", "number")[: per_tick * 2])

    repo_allowed: Dict[int, bool] = {}

    def _rate_allows(repo_id: int) -> bool:
        if repo_id not in repo_allowed:
            allowed = True
            try:
                repo = Repository.objects.get(id=repo_id)
                client = GitHubClient(operation="syncer_pr_read", owner=repo.owner, repo=repo.name)
                snap = get_rate_snapshot(client.token_id) or {}
                remaining_pts = snap.get("remaining")
                if isinstance(remaining_pts, int) and remaining_pts < rate_floor:
                    allowed = False
            except Exception:
                allowed = True
            repo_allowed[repo_id] = allowed
        return repo_allowed[repo_id]

    enqueued = 0
    skipped_dedupe = 0
    skipped_rate = 0
    for repo_id, number in rows:
        if enqueued >= per_tick:
            break
        if not _rate_allows(repo_id):
            skipped_rate += 1
            continue
        key = sync_pr_enqueue_key(repo_id=repo_id, number=int(number))
        if not claim_enqueue_slot(key=key, ttl_seconds=dedupe_ttl):
            skipped_dedupe += 1
            continue
        sync_pr_task.delay(repo_id, int(number), force=True)
        enqueued += 1

    log.info(
        "resync_archive_touched_tick: enqueued=%s skipped_rate=%s skipped_dedupe=%s remaining=%s",
        enqueued,
        skipped_rate,
        skipped_dedupe,
        remaining,
    )
    return {
        "status": "ok" if enqueued else "skipped",
        "enqueued": enqueued,
        "skipped_rate_budget": skipped_rate,
        "skipped_dedupe": skipped_dedupe,
        "remaining": remaining,
        "per_tick": per_tick,
    }
