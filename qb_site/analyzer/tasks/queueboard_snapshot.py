from __future__ import annotations

from celery import shared_task

from analyzer.services.queueboard_snapshot import QueueboardSnapshotBuilder
from core.models import Repository


@shared_task(name="analyzer.build_queueboard_snapshot")
def build_queueboard_snapshot(repository_id: int, cache_key: str = "default") -> int:
    """Build and store a queueboard snapshot for a repository.

    Returns the QueueSnapshot id.
    """
    repo = Repository.objects.get(id=repository_id)
    builder = QueueboardSnapshotBuilder()
    snapshot = builder.build_and_store(repo, cache_key=cache_key)
    return snapshot.id
