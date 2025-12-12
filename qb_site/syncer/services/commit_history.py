from __future__ import annotations

from typing import Callable, List, Optional

from django.db import transaction
from django.utils import timezone

from core.models import Repository
from syncer.models import CommitHistoryHarvest, PullRequest
from syncer.services.github_client import GitHubClient


def harvest_commit_history_shas(
    *,
    client: GitHubClient,
    repo: Repository,
    start_sha: str,
    max_pages: int = 1,
    page_size: int = 20,
    since_iso: Optional[str] = None,
    rate_log: Optional[Callable[[dict], None]] = None,
) -> List[str]:
    """Collect commit SHAs by walking history from a starting SHA backwards."""
    seen: set[str] = set()
    shas: List[str] = []
    pages = 0
    cursor: Optional[str] = None
    while pages < max_pages:
        data = client.get_commit_history_from_sha(
            owner=repo.owner,
            name=repo.name,
            sha=start_sha,
            first=page_size,
            after=cursor,
            since=since_iso,
        )
        if rate_log:
            rl = client.get_last_rate_limit() or {}
            if isinstance(rl, dict):
                rate_log(rl)
        commit_obj = ((data.get("data") or {}).get("repository") or {}).get("object") or {}
        history = (commit_obj.get("history") or {}) if commit_obj.get("__typename") == "Commit" else {}
        nodes = history.get("nodes") or []
        page_info = history.get("pageInfo") or {}
        for node in nodes:
            sha = (node or {}).get("oid") or ""
            if not sha or sha in seen:
                continue
            seen.add(sha)
            shas.append(sha)
        has_next = bool(page_info.get("hasNextPage"))
        cursor = page_info.get("endCursor")
        pages += 1
        if not has_next:
            break
    return shas


@transaction.atomic
def harvest_commit_history_with_cursor(
    *,
    client: GitHubClient,
    pr: PullRequest,
    start_sha: str,
    max_pages: int = 1,
    page_size: int = 20,
    since_iso: Optional[str] = None,
    rate_log: Optional[Callable[[dict], None]] = None,
) -> tuple[List[str], CommitHistoryHarvest]:
    """Harvest commit SHAs using a persisted cursor; returns (shas, cursor_row)."""
    state, _ = CommitHistoryHarvest.objects.select_for_update().get_or_create(
        pull_request=pr,
        start_sha=start_sha,
        defaults={"cursor": None, "has_more": True, "cutoff_ts": None},
    )
    if since_iso and state.cutoff_ts is None:
        try:
            cutoff = timezone.datetime.fromisoformat(since_iso)
            if timezone.is_naive(cutoff):
                cutoff = timezone.make_aware(cutoff)
            state.cutoff_ts = cutoff
        except Exception:
            pass
    if not state.has_more:
        return [], state

    seen: set[str] = set()
    shas: List[str] = []
    pages = 0
    cursor = state.cursor
    while pages < max_pages and state.has_more:
        data = client.get_commit_history_from_sha(
            owner=pr.repository.owner,
            name=pr.repository.name,
            sha=start_sha,
            first=page_size,
            after=cursor,
            since=state.cutoff_ts.isoformat() if state.cutoff_ts else since_iso,
        )
        if rate_log:
            rl = client.get_last_rate_limit() or {}
            if isinstance(rl, dict):
                rate_log(rl)
        commit_obj = ((data.get("data") or {}).get("repository") or {}).get("object") or {}
        history = (commit_obj.get("history") or {}) if commit_obj.get("__typename") == "Commit" else {}
        nodes = history.get("nodes") or []
        page_info = history.get("pageInfo") or {}
        for node in nodes:
            sha = (node or {}).get("oid") or ""
            if not sha or sha in seen:
                continue
            if state.cutoff_ts:
                committed_at = node.get("committedDate")
                if committed_at:
                    try:
                        dt = timezone.datetime.fromisoformat(committed_at)
                        if timezone.is_naive(dt):
                            dt = timezone.make_aware(dt)
                        if dt < state.cutoff_ts:
                            state.has_more = False
                            break
                    except Exception:
                        pass
            seen.add(sha)
            shas.append(sha)
        state.cursor = page_info.get("endCursor")
        state.has_more = bool(page_info.get("hasNextPage")) and state.has_more
        state.attempts += 1
        state.last_harvested_at = timezone.now()
        state.save(update_fields=["cursor", "has_more", "attempts", "last_harvested_at", "updated_at", "cutoff_ts"])
        cursor = state.cursor
        pages += 1

    return shas, state
