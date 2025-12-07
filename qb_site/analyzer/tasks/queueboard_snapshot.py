from __future__ import annotations

from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from analyzer.models import QueueSnapshot
from analyzer.services.queueboard_snapshot import QueueboardSnapshotBuilder
from core.models import Repository


@shared_task(name="analyzer.build_queueboard_snapshot")
def build_queueboard_snapshot(repository_id: int, cache_key: str = "default", expires_in_seconds: int | None = None) -> int:
    """Build and store a queueboard snapshot for a repository.

    Returns the QueueSnapshot id.
    """
    repo = Repository.objects.get(id=repository_id)
    builder = QueueboardSnapshotBuilder()
    expires_at = None
    if expires_in_seconds is not None and int(expires_in_seconds) > 0:
        expires_at = timezone.now() + timedelta(seconds=int(expires_in_seconds))
    snapshot = builder.build_and_store(repo, cache_key=cache_key, expires_at=expires_at)
    return snapshot.id


@shared_task(name="analyzer.refresh_queueboard_snapshots")
def refresh_queueboard_snapshots_task(*, cache_key: str = "default", fanout: bool = True, force: bool = False) -> dict:
    """Refresh queueboard snapshots for all active repositories.

    When ``fanout`` is True (default), enqueue per-repo builds to avoid long-running
    tasks on the beat worker. Existing snapshots are reused unless stale or ``force``
    is requested. Staleness is determined by ``expires_at`` or the configured TTL.
    """
    ttl_seconds = int(getattr(settings, "ANALYZER_QUEUEBOARD_SNAPSHOT_TTL_SECONDS", 0))
    repos = list(Repository.objects.filter(is_active=True).only("id", "owner", "name"))
    now_ts = timezone.now()
    enqueued = 0
    built_inline = 0
    skipped_fresh = 0
    per_repo: list[dict] = []

    for repo in repos:
        existing = (
            QueueSnapshot.objects.filter(repository=repo, cache_key=cache_key)
            .only("id", "generated_at", "expires_at")
            .order_by("-generated_at", "-id")
            .first()
        )
        stale = existing is None
        if existing:
            if existing.expires_at and existing.expires_at <= now_ts:
                stale = True
            elif ttl_seconds > 0 and existing.generated_at <= now_ts - timedelta(seconds=ttl_seconds):
                stale = True

        if not force and existing and not stale:
            skipped_fresh += 1
            per_repo.append(
                {
                    "repo": f"{repo.owner}/{repo.name}",
                    "status": "fresh",
                    "snapshot_id": existing.id,
                }
            )
            continue

        expires_in = ttl_seconds if ttl_seconds > 0 else None
        if fanout:
            async_res = build_queueboard_snapshot.delay(
                repository_id=repo.id,
                cache_key=cache_key,
                expires_in_seconds=expires_in,
            )
            enqueued += 1
            per_repo.append(
                {
                    "repo": f"{repo.owner}/{repo.name}",
                    "status": "enqueued",
                    "task_id": getattr(async_res, "id", None),
                    "expires_in_seconds": expires_in,
                }
            )
        else:
            builder = QueueboardSnapshotBuilder()
            expires_at = None
            if expires_in:
                expires_at = timezone.now() + timedelta(seconds=int(expires_in))
            snapshot = builder.build_and_store(repo, cache_key=cache_key, expires_at=expires_at)
            built_inline += 1
            per_repo.append(
                {
                    "repo": f"{repo.owner}/{repo.name}",
                    "status": "built",
                    "snapshot_id": snapshot.id,
                    "expires_at": snapshot.expires_at,
                }
            )

    return {
        "repos": len(repos),
        "cache_key": cache_key,
        "ttl_seconds": ttl_seconds,
        "fanout": bool(fanout),
        "force": bool(force),
        "enqueued": enqueued,
        "built_inline": built_inline,
        "skipped_fresh": skipped_fresh,
        "per_repo": per_repo,
    }
