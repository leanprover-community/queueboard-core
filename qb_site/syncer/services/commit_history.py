from __future__ import annotations

from typing import List, Optional

from core.models import Repository
from syncer.services.github_client import GitHubClient


def harvest_commit_history_shas(
    *,
    client: GitHubClient,
    repo: Repository,
    start_sha: str,
    max_pages: int = 1,
    page_size: int = 20,
    since_iso: Optional[str] = None,
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
