from __future__ import annotations

import logging
import resource
import sys
from datetime import timezone
from datetime import datetime
from typing import Optional

from celery.signals import before_task_publish, task_postrun, task_prerun
from django_celery_results.models import TaskResult

from core.models import TaskResultLink


log = logging.getLogger(__name__)


def _rss_mb() -> Optional[float]:
    """Return current process RSS in MB (best-effort).

    ru_maxrss units differ by platform: kilobytes on Linux, bytes on macOS.
    """
    try:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        raw = float(getattr(usage, "ru_maxrss", 0.0))
        if raw <= 0:
            return None
        if sys.platform == "darwin":
            return raw / (1024.0 * 1024.0)  # bytes -> MB
        # Linux and most other Unix report kilobytes
        return raw / 1024.0  # kB -> MB
    except Exception:
        return None


def _store_task_links(task_id: str | None, parent_id: str | None, root_id: str | None) -> None:
    if not task_id:
        return
    try:
        tr = TaskResult.objects.filter(task_id=task_id).first()
        if tr is None:
            return
        TaskResultLink.objects.update_or_create(
            task=tr,
            defaults={
                "parent_task_id": parent_id or None,
                "root_task_id": root_id or parent_id or None,
            },
        )
    except Exception:
        # Best-effort; avoid breaking task execution on failures
        return


@task_postrun.connect
def capture_task_links(sender=None, task_id=None, task=None, **kwargs):  # pragma: no cover - integration hook
    """Capture parent/root ids for every completed task."""
    parent_id = None
    root_id = None
    try:
        req = getattr(task, "request", None)
        parent_id = getattr(req, "parent_id", None) if req else None
        root_id = getattr(req, "root_id", None) if req else None
    except Exception:
        parent_id = None
        root_id = None
    _store_task_links(task_id, parent_id, root_id)


def enqueue_with_parent(sig, request, **apply_kwargs):
    """Apply a task signature forwarding parent/root headers if present.

    Use this when a task enqueues another task with .delay() semantics but we want
    parent/root tracking in TaskResultLink without switching to canvas chains/groups.
    """
    try:
        headers = {}
        if getattr(request, "id", None):
            headers["parent_id"] = request.id
        root_id = getattr(request, "root_id", None)
        if root_id or headers.get("parent_id"):
            headers["root_id"] = root_id or headers.get("parent_id")
        if headers:
            return sig.apply_async(headers=headers, **apply_kwargs)
        return sig.apply_async(**apply_kwargs)
    except Exception:
        return sig.apply_async(**apply_kwargs)


@task_prerun.connect
def log_task_rss_prerun(sender=None, task_id=None, task=None, **kwargs):  # pragma: no cover - integration hook
    """Log best-effort RSS before a task runs."""
    try:
        rss_mb = _rss_mb()
        if rss_mb is not None:
            log.info("celery_rss event=prerun task=%s id=%s rss_mb=%.1f", getattr(task, "name", sender), task_id, rss_mb)
    except Exception:
        return


@task_postrun.connect
def log_task_rss_postrun(sender=None, task_id=None, task=None, **kwargs):  # pragma: no cover - integration hook
    """Log best-effort RSS after a task finishes."""
    try:
        rss_mb = _rss_mb()
        if rss_mb is not None:
            log.info("celery_rss event=postrun task=%s id=%s rss_mb=%.1f", getattr(task, "name", sender), task_id, rss_mb)
    except Exception:
        return


@before_task_publish.connect
def stamp_enqueue_headers(headers=None, **kwargs):  # pragma: no cover - integration hook
    """Attach best-effort publish-time metadata to task headers."""
    if not isinstance(headers, dict):
        return
    try:
        headers.setdefault("qb_enqueued_at", datetime.now(tz=timezone.utc).isoformat())
    except Exception:
        return
