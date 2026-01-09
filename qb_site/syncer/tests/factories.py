from __future__ import annotations

from typing import Any, Optional

from django.utils import timezone

from core.models import Repository
from syncer.models import PullRequest


def make_repo(
    *,
    owner: str = "o",
    name: str = "r",
    default_branch: str = "master",
    is_active: bool = True,
    **overrides: Any,
) -> Repository:
    data: dict[str, Any] = {
        "owner": owner,
        "name": name,
        "default_branch": default_branch,
        "is_active": is_active,
    }
    data.update(overrides)
    return Repository.objects.create(**data)


def make_pr(
    repo: Repository,
    number: int,
    *,
    state: str = "open",
    is_draft: bool = False,
    gh_created_at: Optional[Any] = None,
    gh_updated_at: Optional[Any] = None,
    base_ref_name: str = "master",
    head_ref_name: str = "b",
    head_sha: Optional[str] = "a" * 40,
    head_repo_owner_login: str = "o",
    head_repo_name: str = "fork",
    title: str = "t",
    body: str = "",
    additions: int = 0,
    deletions: int = 0,
    changed_files_count: int = 0,
    last_synced_at: Optional[Any] = None,
    author: Any = None,
    closed_at: Any = None,
    merged_at: Any = None,
    **overrides: Any,
) -> PullRequest:
    now = timezone.now()
    data: dict[str, Any] = {
        "repository": repo,
        "number": number,
        "author": author,
        "state": state,
        "is_draft": is_draft,
        "gh_created_at": gh_created_at if gh_created_at is not None else now,
        "gh_updated_at": gh_updated_at if gh_updated_at is not None else now,
        "closed_at": closed_at,
        "merged_at": merged_at,
        "base_ref_name": base_ref_name,
        "head_ref_name": head_ref_name,
        "head_sha": head_sha,
        "head_repo_owner_login": head_repo_owner_login,
        "head_repo_name": head_repo_name,
        "title": title,
        "body": body,
        "additions": additions,
        "deletions": deletions,
        "changed_files_count": changed_files_count,
    }
    # Only set last_synced_at when provided to mirror existing tests precisely
    if last_synced_at is not None:
        data["last_synced_at"] = last_synced_at
    data.update(overrides)
    return PullRequest.objects.create(**data)
