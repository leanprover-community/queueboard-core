from __future__ import annotations

from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from analyzer.models import QueueRuleSet
from analyzer.services.reviewer_assignment import AreaStatsBuilder, ReviewerAssignmentBuilder
from core.models import Repository


@shared_task(name="analyzer.build_reviewer_assignment")
def build_reviewer_assignment(
    repository_id: int,
    cache_key: str | None = None,
    expires_in_seconds: int | None = None,
    rule_set_id: int | None = None,
) -> int:
    """Build and store reviewer assignment snapshot for a repository."""
    repo = Repository.objects.get(id=repository_id)
    rule_set = None
    if rule_set_id is not None:
        rule_set = QueueRuleSet.objects.filter(id=rule_set_id, repository=repo).first()
    builder = ReviewerAssignmentBuilder()
    expires_at = None
    if expires_in_seconds is not None and int(expires_in_seconds) > 0:
        expires_at = timezone.now() + timedelta(seconds=int(expires_in_seconds))
    effective_cache_key = cache_key or (str(rule_set.id) if rule_set else "default")
    snapshot = builder.build_and_store(
        repo,
        cache_key=effective_cache_key,
        expires_at=expires_at,
        rule_set=rule_set,
    )
    return snapshot.id


@shared_task(name="analyzer.refresh_reviewer_assignments")
def refresh_reviewer_assignments_task(*, cache_key: str = "default", fanout: bool = True, force: bool = False) -> dict:
    """Refresh reviewer assignment snapshots for all active repositories."""
    ttl_seconds = int(getattr(settings, "ANALYZER_REVIEWER_ASSIGNMENT_TTL_SECONDS", 0))
    repos = list(Repository.objects.filter(is_active=True).only("id", "owner", "name"))
    now_ts = timezone.now()
    enqueued = 0
    built_inline = 0
    skipped_fresh = 0
    per_repo: list[dict] = []

    for repo in repos:
        rule_sets = list(QueueRuleSet.objects.filter(repository=repo, is_active=True))
        if not rule_sets:
            rule_sets = [None]

        for rule_set in rule_sets:
            rs_cache_key = str(rule_set.id) if rule_set else cache_key
            existing = (
                repo.reviewer_assignment_snapshots.filter(cache_key=rs_cache_key)
                .only("id", "generated_at", "expires_at")
                .order_by("-generated_at", "-id")
                .first()
            )
            rs_stale = existing is None
            if existing:
                if existing.expires_at and existing.expires_at <= now_ts:
                    rs_stale = True
                elif ttl_seconds > 0 and existing.generated_at <= now_ts - timedelta(seconds=ttl_seconds):
                    rs_stale = True

            if not force and existing and not rs_stale:
                skipped_fresh += 1
                per_repo.append(
                    {
                        "repo": f"{repo.owner}/{repo.name}",
                        "rule_set_id": rule_set.id if rule_set else None,
                        "status": "fresh",
                        "snapshot_id": existing.id,
                    }
                )
                continue

            expires_in = ttl_seconds if ttl_seconds > 0 else None
            if fanout:
                async_res = build_reviewer_assignment.delay(
                    repository_id=repo.id,
                    cache_key=rs_cache_key,
                    expires_in_seconds=expires_in,
                    rule_set_id=rule_set.id if rule_set else None,
                )
                enqueued += 1
                per_repo.append(
                    {
                        "repo": f"{repo.owner}/{repo.name}",
                        "rule_set_id": rule_set.id if rule_set else None,
                        "status": "enqueued",
                        "task_id": getattr(async_res, "id", None),
                        "expires_in_seconds": expires_in,
                    }
                )
            else:
                builder = ReviewerAssignmentBuilder()
                expires_at = None
                if expires_in:
                    expires_at = timezone.now() + timedelta(seconds=int(expires_in))
                snapshot = builder.build_and_store(
                    repo,
                    cache_key=rs_cache_key,
                    expires_at=expires_at,
                    rule_set=rule_set,
                )
                built_inline += 1
                per_repo.append(
                    {
                        "repo": f"{repo.owner}/{repo.name}",
                        "rule_set_id": rule_set.id if rule_set else None,
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


@shared_task(name="analyzer.build_area_stats")
def build_area_stats(
    repository_id: int,
    cache_key: str | None = None,
    expires_in_seconds: int | None = None,
    rule_set_id: int | None = None,
) -> int:
    """Build and store area stats snapshot for a repository."""
    repo = Repository.objects.get(id=repository_id)
    rule_set = None
    if rule_set_id is not None:
        rule_set = QueueRuleSet.objects.filter(id=rule_set_id, repository=repo).first()
    builder = AreaStatsBuilder()
    expires_at = None
    if expires_in_seconds is not None and int(expires_in_seconds) > 0:
        expires_at = timezone.now() + timedelta(seconds=int(expires_in_seconds))
    effective_cache_key = cache_key or (str(rule_set.id) if rule_set else "default")
    snapshot = builder.build_and_store(
        repo,
        cache_key=effective_cache_key,
        expires_at=expires_at,
        rule_set=rule_set,
    )
    return snapshot.id


@shared_task(name="analyzer.refresh_area_stats")
def refresh_area_stats_task(*, cache_key: str = "default", fanout: bool = True, force: bool = False) -> dict:
    """Refresh area stats snapshots for all active repositories."""
    ttl_seconds = int(
        getattr(
            settings,
            "ANALYZER_AREA_STATS_TTL_SECONDS",
            getattr(settings, "ANALYZER_QUEUEBOARD_SNAPSHOT_TTL_SECONDS", 0),
        )
    )
    repos = list(Repository.objects.filter(is_active=True).only("id", "owner", "name"))
    now_ts = timezone.now()
    enqueued = 0
    built_inline = 0
    skipped_fresh = 0
    per_repo: list[dict] = []

    for repo in repos:
        rule_sets = list(QueueRuleSet.objects.filter(repository=repo, is_active=True))
        if not rule_sets:
            rule_sets = [None]

        for rule_set in rule_sets:
            rs_cache_key = str(rule_set.id) if rule_set else cache_key
            existing = (
                repo.area_stats_snapshots.filter(cache_key=rs_cache_key)
                .only("id", "generated_at", "expires_at")
                .order_by("-generated_at", "-id")
                .first()
            )
            rs_stale = existing is None
            if existing:
                if existing.expires_at and existing.expires_at <= now_ts:
                    rs_stale = True
                elif ttl_seconds > 0 and existing.generated_at <= now_ts - timedelta(seconds=ttl_seconds):
                    rs_stale = True

            if not force and existing and not rs_stale:
                skipped_fresh += 1
                per_repo.append(
                    {
                        "repo": f"{repo.owner}/{repo.name}",
                        "rule_set_id": rule_set.id if rule_set else None,
                        "status": "fresh",
                        "snapshot_id": existing.id,
                    }
                )
                continue

            expires_in = ttl_seconds if ttl_seconds > 0 else None
            if fanout:
                async_res = build_area_stats.delay(
                    repository_id=repo.id,
                    cache_key=rs_cache_key,
                    expires_in_seconds=expires_in,
                    rule_set_id=rule_set.id if rule_set else None,
                )
                enqueued += 1
                per_repo.append(
                    {
                        "repo": f"{repo.owner}/{repo.name}",
                        "rule_set_id": rule_set.id if rule_set else None,
                        "status": "enqueued",
                        "task_id": getattr(async_res, "id", None),
                        "expires_in_seconds": expires_in,
                    }
                )
            else:
                builder = AreaStatsBuilder()
                expires_at = None
                if expires_in:
                    expires_at = timezone.now() + timedelta(seconds=int(expires_in))
                snapshot = builder.build_and_store(
                    repo,
                    cache_key=rs_cache_key,
                    expires_at=expires_at,
                    rule_set=rule_set,
                )
                built_inline += 1
                per_repo.append(
                    {
                        "repo": f"{repo.owner}/{repo.name}",
                        "rule_set_id": rule_set.id if rule_set else None,
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
