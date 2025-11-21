from __future__ import annotations

from django.db import models

from django_celery_results.models import TaskResult


class TaskResultLink(models.Model):
    """Sidecar to store parent/root task ids for celery TaskResults."""

    task = models.OneToOneField(
        TaskResult,
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="link",
    )
    parent_task_id = models.CharField(max_length=255, null=True, blank=True, db_index=True)
    root_task_id = models.CharField(max_length=255, null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["parent_task_id"]),
            models.Index(fields=["root_task_id"]),
        ]

    def __str__(self) -> str:  # pragma: no cover - display
        return f"TaskResultLink<{self.task_id}>"
