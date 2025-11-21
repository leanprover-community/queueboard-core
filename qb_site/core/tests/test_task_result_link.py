from __future__ import annotations

from django.test import TestCase

from django_celery_results.models import TaskResult

from core.models import TaskResultLink
from core.celery_signals import _store_task_links


class TaskResultLinkTests(TestCase):
    def test_store_task_links_creates_and_updates(self) -> None:
        tr = TaskResult.objects.create(task_id="task-123", task_name="x.y", status="SUCCESS")

        _store_task_links(task_id=tr.task_id, parent_id="parent-1", root_id="root-1")
        link = TaskResultLink.objects.get(task=tr)
        self.assertEqual(link.parent_task_id, "parent-1")
        self.assertEqual(link.root_task_id, "root-1")

        # Update existing sidecar and ensure root falls back to parent when missing
        _store_task_links(task_id=tr.task_id, parent_id="parent-2", root_id=None)
        link.refresh_from_db()
        self.assertEqual(link.parent_task_id, "parent-2")
        self.assertEqual(link.root_task_id, "parent-2")
