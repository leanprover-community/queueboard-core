from __future__ import annotations

from celery.signals import task_postrun
from django_celery_results.models import TaskResult

from core.models import TaskResultLink


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
