from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

from celery import shared_task
from django.conf import settings
from django.db.models import DateTimeField, ExpressionWrapper, F, Q
from django.utils import timezone

from core.models import Repository
from syncer.models import PullRequest, RepoBackfillCursor
from syncer.services.github_client import GitHubClient
from .sync_tasks import sync_pr_task


@shared_task(name="syncer.backfill_repo_history")
def backfill_repo_history_task(  # type: ignore[no-redef]
    repo_id: int,
    *,
    page_size: Optional[int] = None,
    max_pages: Optional[int] = None,
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
    rate_events: list[dict] = []
    rl_snapshot: Dict[str, Any] = {}

    # Resolve effective page_size and max_pages from settings when not explicitly provided.
    eff_page_size = int(page_size) if page_size is not None else int(getattr(settings, "SYNCER_HISTORY_BACKFILL_PAGE_SIZE", 50))
    eff_max_pages = int(max_pages) if max_pages is not None else int(getattr(settings, "SYNCER_HISTORY_BACKFILL_MAX_PAGES", 1))

    # Determine which states to backfill; default to full history coverage.
    if states is None:
        raw_states = getattr(settings, "SYNCER_HISTORY_BACKFILL_STATES_DEFAULT", ["OPEN", "MERGED", "CLOSED"])
        if isinstance(raw_states, (list, tuple, set)):
            st: list[str] = [str(s).upper() for s in raw_states if s]
        else:
            st = [s.strip().upper() for s in str(raw_states).split(",") if s.strip()]
    else:
        st = [str(s).upper() for s in states if s]

    after: Optional[str] = cursor.created_cursor
    oldest_seen_created_at = cursor.oldest_created_at

    while used_pages < eff_max_pages:
        data = client.get_prs_created_page(
            owner=repo.owner,
            name=repo.name,
            first=int(max(1, min(eff_page_size, 100))),
            after=after,
            states=st,
        )
        rl = client.get_last_rate_limit() or {}
        if isinstance(rl, dict):
            rl_snapshot = rl
            try:
                rate_events.append(
                    {
                        "label": "prs_created_page",
                        "cost": rl.get("cost"),
                        "remaining": rl.get("remaining"),
                        "resetAt": rl.get("resetAt"),
                    }
                )
            except Exception:
                pass
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
        "rate_events": rate_events,
        "rate_limit": rl_snapshot,
    }


@shared_task(name="syncer.backfill_repo_history_active")
def backfill_repo_history_active_task() -> Dict[str, Any]:  # type: ignore[no-redef]
    """Enqueue history backfill for all active repositories.

    Intended for periodic scheduling via Celery beat; iterates active repositories
    and runs a small slice of createdAt-based backfill for each.
    """
    repos = list(Repository.objects.filter(is_active=True).only("id", "owner", "name"))
    enqueued = 0
    for r in repos:
        backfill_repo_history_task.delay(r.id)
        enqueued += 1
    return {"repos": len(repos), "enqueued": enqueued}


@shared_task(name="syncer.backfill_repo_incomplete_prs")
def backfill_repo_incomplete_prs_task(  # type: ignore[no-redef]
    repo_id: int,
    *,
    limit: int = 50,
    states: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Backfill incomplete PRs for a repository.

    Behavior
    - Select PullRequest rows that have either timeline or commits backfill incomplete.
    - Optionally filter by coarse PR state using a GitHub-style states list
      (OPEN/MERGED/CLOSED), mapped onto the local `state` field (`open`/`closed`).
    - Order by most recently updated first and enqueue a bounded number of `sync_pr`
      tasks per run, using the configured backfill page budgets.
    """
    repo = Repository.objects.get(id=int(repo_id))

    # Normalize requested states (OPEN / MERGED / CLOSED) to local DB values (open / closed)
    if states is None:
        result_states: list[str] = ["OPEN", "MERGED", "CLOSED"]
        db_states: Optional[set[str]] = None
    else:
        result_states = [str(s).upper() for s in states if s]
        db_states = set()
        for raw in result_states:
            if raw == "OPEN":
                db_states.add("open")
            elif raw in {"CLOSED", "MERGED"}:
                db_states.add("closed")
        if not db_states:
            # Caller requested only unsupported states; nothing to do.
            return {
                "repo": f"{repo.owner}/{repo.name}",
                "repo_id": repo.id,
                "enqueued": 0,
                "remaining": 0,
                "states": result_states,
            }

    queryset = PullRequest.objects.filter(repository=repo)
    if db_states is not None:
        queryset = queryset.filter(state__in=list(db_states))

    eps = int(getattr(settings, "SYNCER_LAST_SYNC_EPSILON_SECONDS", 300))
    stale_cutoff = ExpressionWrapper(
        F("gh_updated_at") - timezone.timedelta(seconds=max(0, eps)),
        output_field=DateTimeField(),
    )
    queryset = queryset.filter(
        Q(timeline_backfill_done=False)
        | Q(commits_backfill_done=False)
        | Q(last_synced_at__isnull=True)
        | Q(last_synced_at__lt=stale_cutoff)
    )
    total_incomplete = queryset.count()

    limit_int = int(limit)
    if limit_int <= 0 or total_incomplete == 0:
        return {
            "repo": f"{repo.owner}/{repo.name}",
            "repo_id": repo.id,
            "enqueued": 0,
            "remaining": total_incomplete,
            "states": result_states,
        }

    candidates = list(queryset.order_by("-gh_updated_at", "-id")[:limit_int])

    backfill_timeline_pages = int(getattr(settings, "SYNCER_TIMELINE_BACKFILL_PAGES", 1))
    backfill_commit_pages = int(getattr(settings, "SYNCER_COMMITS_BACKFILL_PAGES", 1))

    enqueued = 0
    for pr in candidates:
        sync_pr_task.delay(
            repo.id,
            int(pr.number),
            backfill_timeline_pages=backfill_timeline_pages,
            backfill_commit_pages=backfill_commit_pages,
        )
        enqueued += 1

    remaining_after = max(total_incomplete - enqueued, 0)
    return {
        "repo": f"{repo.owner}/{repo.name}",
        "repo_id": repo.id,
        "enqueued": enqueued,
        "remaining": remaining_after,
        "backlog": total_incomplete,
        "states": result_states,
    }


@shared_task(name="syncer.backfill_repo_incomplete_prs_active")
def backfill_repo_incomplete_prs_active_task(  # type: ignore[no-redef]
    limit: int = 50,
    states: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Enqueue incomplete PR backfill for all active repositories."""
    repos = list(Repository.objects.filter(is_active=True).only("id", "owner", "name"))
    enqueued = 0
    for repo in repos:
        backfill_repo_incomplete_prs_task.delay(repo.id, limit=limit, states=states)
        enqueued += 1
    return {"repos": len(repos), "enqueued": enqueued, "limit": int(limit)}


@shared_task(name="syncer.backfill_repo_engagement")
def backfill_repo_engagement_task(  # type: ignore[no-redef]
    repo_id: int,
    *,
    limit: int = 50,
    states: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Enqueue PR syncs to populate engagement fields (files/assignees/comments/approvals) and head CI rollup.

    Targets PRs missing engagement data or head rollup (engagement_synced_at/head_ci_state is null).
    """
    repo = Repository.objects.get(id=int(repo_id))

    # Normalize requested states (OPEN / MERGED / CLOSED) to local DB values (open / closed)
    if states is None:
        result_states: list[str] = ["OPEN", "MERGED", "CLOSED"]
        db_states: Optional[set[str]] = None
    else:
        result_states = [str(s).upper() for s in states if s]
        db_states = set()
        for raw in result_states:
            if raw == "OPEN":
                db_states.add("open")
            elif raw in {"CLOSED", "MERGED"}:
                db_states.add("closed")
        if not db_states:
            return {
                "repo": f"{repo.owner}/{repo.name}",
                "repo_id": repo.id,
                "enqueued": 0,
                "remaining": 0,
                "states": result_states,
            }

    queryset = PullRequest.objects.filter(repository=repo)
    if db_states is not None:
        queryset = queryset.filter(state__in=list(db_states))

    queryset = queryset.filter(Q(engagement_synced_at__isnull=True) | Q(head_ci_state__isnull=True))
    total_needs_engagement = queryset.count()

    limit_int = int(limit)
    if limit_int <= 0 or total_needs_engagement == 0:
        return {
            "repo": f"{repo.owner}/{repo.name}",
            "repo_id": repo.id,
            "enqueued": 0,
            "remaining": total_needs_engagement,
            "states": result_states,
        }

    candidates: list[PullRequest] = []
    # Prefer open PRs first when they are in scope.
    if db_states is None or "open" in db_states:
        open_qs = queryset.filter(state="open").order_by("-gh_updated_at", "-id")[:limit_int]
        candidates.extend(open_qs)
    remaining = max(limit_int - len(candidates), 0)
    if remaining > 0:
        qs_rest = queryset.exclude(state="open").order_by("-gh_updated_at", "-id")[:remaining]
        candidates.extend(qs_rest)

    enqueued = 0
    for pr in candidates:
        sync_pr_task.delay(repo.id, int(pr.number))
        enqueued += 1

    remaining_after = max(total_needs_engagement - enqueued, 0)
    return {
        "repo": f"{repo.owner}/{repo.name}",
        "repo_id": repo.id,
        "enqueued": enqueued,
        "remaining": remaining_after,
        "backlog": total_needs_engagement,
        "states": result_states,
    }


@shared_task(name="syncer.backfill_repo_engagement_active")
def backfill_repo_engagement_active_task(  # type: ignore[no-redef]
    limit: int = 50,
    states: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Enqueue engagement backfill for all active repositories."""
    repos = list(Repository.objects.filter(is_active=True).only("id", "owner", "name"))
    enqueued = 0
    for repo in repos:
        backfill_repo_engagement_task.delay(repo.id, limit=limit, states=states)
        enqueued += 1
    return {"repos": len(repos), "enqueued": enqueued, "limit": int(limit)}
