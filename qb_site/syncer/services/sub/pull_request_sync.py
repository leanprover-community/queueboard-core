from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Tuple

from dateutil import parser as dtparser
from django.utils import timezone

from core.models.repository import Repository
from core.models.user import User
from .core_entities_sync import upsert_user_from_github
from syncer.models.pull_request import PullRequest


@dataclass
class PullRequestUpsertResult:
    pr: PullRequest
    created: bool
    updated_fields: Tuple[str, ...]


def _parse_iso(val: str | None):
    if not val:
        return None
    dt = dtparser.isoparse(val)
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    return dt


def upsert_pull_request(bundle: Dict[str, Any], repo: Repository) -> PullRequestUpsertResult:
    """Upsert the PullRequest row from a parsed PR bundle.

    Expected bundle shape (subset):
        {
          "number": int,
          "state": str,              # "OPEN"/"CLOSED"
          "isDraft": bool,
          "title": str,
          "body": str,
          "createdAt": str,
          "updatedAt": str,
          "baseRefName": str,
          "headRefName": str,
          "headRepositoryOwner": {"login": str},
          "headRepository": {"name": str},
          "additions": int,
          "deletions": int,
          "changedFiles": int,
          "author": {"login": str | None}
        }

    This function should:
      - Resolve/create the author User row by case-insensitive github_login when present.
      - Upsert the PullRequest keyed by (repo, number).
      - Map GitHub timestamps to gh_* fields.

    Returns a PullRequestUpsertResult with the instance and whether it was created.
    """
    # Resolve author if present
    author = bundle.get("author") or {}
    author_obj, _, _ = upsert_user_from_github(author, create_missing=True)

    number = int(bundle.get("number", 0))
    pr = PullRequest.objects.filter(repository=repo, number=number).first()
    created = False
    if pr is None:
        pr = PullRequest(repository=repo, number=number, author=author_obj)
        created = True

    before = {
        "author_id": getattr(pr.author, "id", None),
        "state": pr.state,
        "is_draft": pr.is_draft,
        "title": pr.title,
        "body": pr.body,
        "gh_created_at": pr.gh_created_at,
        "gh_updated_at": pr.gh_updated_at,
        "base_ref_name": pr.base_ref_name,
        "head_ref_name": pr.head_ref_name,
        "head_repo_owner_login": pr.head_repo_owner_login,
        "head_repo_name": pr.head_repo_name,
        "additions": pr.additions,
        "deletions": pr.deletions,
        "changed_files_count": pr.changed_files_count,
    }

    # Map core fields
    pr.author = author_obj
    pr.state = str(bundle.get("state", "OPEN")).lower()
    pr.is_draft = bool(bundle.get("isDraft", False))
    pr.title = bundle.get("title") or ""
    pr.body = bundle.get("body") or ""
    pr.gh_created_at = _parse_iso(bundle.get("createdAt")) or timezone.now()
    pr.gh_updated_at = _parse_iso(bundle.get("updatedAt")) or pr.gh_created_at
    pr.closed_at = _parse_iso(bundle.get("closedAt"))
    pr.merged_at = _parse_iso(bundle.get("mergedAt"))
    pr.base_ref_name = bundle.get("baseRefName") or ""
    pr.head_ref_name = bundle.get("headRefName") or ""
    pr.head_repo_owner_login = (bundle.get("headRepositoryOwner") or {}).get("login", "")
    pr.head_repo_name = (bundle.get("headRepository") or {}).get("name", "")
    pr.additions = int(bundle.get("additions", 0))
    pr.deletions = int(bundle.get("deletions", 0))
    pr.changed_files_count = int(bundle.get("changedFiles", 0))
    pr.last_synced_at = timezone.now()
    pr.save()

    after = {
        "author_id": getattr(pr.author, "id", None),
        "state": pr.state,
        "is_draft": pr.is_draft,
        "title": pr.title,
        "body": pr.body,
        "gh_created_at": pr.gh_created_at,
        "gh_updated_at": pr.gh_updated_at,
        "base_ref_name": pr.base_ref_name,
        "head_ref_name": pr.head_ref_name,
        "head_repo_owner_login": pr.head_repo_owner_login,
        "head_repo_name": pr.head_repo_name,
        "additions": pr.additions,
        "deletions": pr.deletions,
        "changed_files_count": pr.changed_files_count,
    }
    updated_fields = tuple(k for k in after if after[k] != before.get(k))
    return PullRequestUpsertResult(pr=pr, created=created, updated_fields=updated_fields)
