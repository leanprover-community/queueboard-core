from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

from celery import shared_task
from django.utils import timezone

from core.models import Repository
from syncer.models import RepoBackfillCursor
from syncer.services.github_client import GitHubClient
from .sync_tasks import sync_pr_task


@shared_task(name="syncer.backfill_repo_history")
def backfill_repo_history_task(  # type: ignore[no-redef]
    repo_id: int,
    *,
    page_size: int = 50,
    max_pages: int = 1,
    states: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Backfill PR history for a repository by createdAt (oldest first).

    Behavior
    - Uses RepoBackfillCursor to track a GraphQL cursor over pullRequests
      ordered by CREATED_AT ASC.
    - For each discovered PR number, enqueues a normal sync_pr_task to ingest it.
    - Respects an explicit `states` override (e.g., ["OPEN"]) when provided;
      otherwise defaults to ["OPEN","MERGED","CLOSED"] for historical coverage.
    - Stops after `max_pages` pages or when history is exhausted.
    """
    repo = Repository.objects.get(id=int(repo_id))
    cursor, _ = RepoBackfillCursor.objects.get_or_create(repository=repo)

    client = GitHubClient()
    used_pages = 0
    enqueued = 0

    # Determine which states to backfill; default to full history coverage.
    if states is None:
        st: list[str] = ["OPEN", "MERGED", "CLOSED"]
    else:
        st = [str(s).upper() for s in states if s]

    after: Optional[str] = cursor.created_cursor
    oldest_seen_created_at = cursor.oldest_created_at

    while used_pages < int(max_pages):
        data = client.get_prs_created_page(
            owner=repo.owner,
            name=repo.name,
            first=int(max(1, min(page_size, 100))),
            after=after,
            states=st,
        )
        repo_node = (data.get("data") or {}).get("repository") or {}
        conn = repo_node.get("pullRequests") or {}
        nodes = conn.get("nodes") or []
        if not nodes:
            cursor.completed = True
            cursor.last_run_at = timezone.now()
            cursor.save(update_fields=["completed", "last_run_at"])
            break

        for n in nodes:
            try:
                number = int(n.get("number"))
            except Exception:
                continue
            sync_pr_task.delay(repo.id, number)
            enqueued += 1

        # Track oldest createdAt we have seen (for visibility only)
        try:
            # nodes are in createdAt ASC; first node is the oldest on this page
            created_at_str = nodes[0].get("createdAt")
            if created_at_str:
                from dateutil import parser as _dtp

                created_dt = _dtp.isoparse(created_at_str)
                if timezone.is_naive(created_dt):
                    created_dt = timezone.make_aware(created_dt)
                if oldest_seen_created_at is None or created_dt < oldest_seen_created_at:
                    oldest_seen_created_at = created_dt
        except Exception:
            pass

        pinfo = conn.get("pageInfo") or {}
        after = pinfo.get("endCursor")
        cursor.created_cursor = after
        cursor.oldest_created_at = oldest_seen_created_at
        cursor.last_run_at = timezone.now()
        has_next = bool(pinfo.get("hasNextPage"))
        cursor.completed = not has_next
        cursor.save(update_fields=["created_cursor", "oldest_created_at", "completed", "last_run_at"])

        used_pages += 1
        if not has_next:
            break

    return {
        "skipped": False,
        "repo": f"{repo.owner}/{repo.name}",
        "repo_id": repo.id,
        "pages_used": used_pages,
        "enqueued": enqueued,
        "completed": bool(cursor.completed),
        "states": st,
    }


@shared_task(name="syncer.backfill_repo_history_active")
def backfill_repo_history_active_task() -> Dict[str, Any]:  # type: ignore[no-redef]
    """Enqueue history backfill for all active repositories.

    Intended for periodic scheduling via Celery beat; iterates active repositories
    and runs a small slice of createdAt-based backfill for each.
    """
    from core.models import Repository  # local import to avoid circulars

    repos = list(Repository.objects.filter(is_active=True).only("id", "owner", "name"))
    enqueued = 0
    for r in repos:
        backfill_repo_history_task.delay(r.id)
        enqueued += 1
    return {"repos": len(repos), "enqueued": enqueued}
