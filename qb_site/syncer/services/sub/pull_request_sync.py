from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Any, Dict, Tuple

from dateutil import parser as dtparser
from django.utils import timezone

from core.models.repository import Repository
from core.utils.db import update_if_changed
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


def upsert_pull_request(
    bundle: Dict[str, Any],
    repo: Repository,
    *,
    if_newer_than: _dt.datetime | None = None,
    skip_watermark: bool = False,
) -> PullRequestUpsertResult:
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
          "headRefOid": str,
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

    Optional archive-mode parameters (design doc 043):
      - ``if_newer_than``: when set and the existing row's ``gh_updated_at``
        is later than the snapshot, the *entire* core update is skipped — an
        older snapshot can never carry a more-recent core value, so it must
        not regress any field. The create path and the archive-is-newer case
        are unaffected (all fields flow through). (Originally only a subset
        of "primary semantic" fields was gated while additions/deletions/
        refs/gh_updated_at flowed through; that silently rewound those on
        closed PRs the archive touched but that never get resynced — see
        design doc 043 §Labels follow-up.)
      - ``skip_watermark``: when True, do NOT set ``last_synced_at`` on the
        create path. Required by the archive importer so the live discovery
        preflight (``gh_updated_at > last_synced_at``) can still pick the
        PR up later for timeline-page backfill.

    Returns a PullRequestUpsertResult with the instance and whether it was created.
    """
    # Resolve author if present
    author = bundle.get("author") or {}
    author_obj, _, _ = upsert_user_from_github(author, create_missing=True)

    number = int(bundle.get("number", 0))
    pr = PullRequest.objects.filter(repository=repo, number=number).first()

    core_values = {
        "author": author_obj,
        "state": str(bundle.get("state", "OPEN")).lower(),
        "is_draft": bool(bundle.get("isDraft", False)),
        "title": bundle.get("title") or "",
        "body": bundle.get("body") or "",
        "gh_created_at": _parse_iso(bundle.get("createdAt")) or timezone.now(),
        "gh_updated_at": _parse_iso(bundle.get("updatedAt")) or _parse_iso(bundle.get("createdAt")) or timezone.now(),
        "closed_at": _parse_iso(bundle.get("closedAt")),
        "merged_at": _parse_iso(bundle.get("mergedAt")),
        "base_ref_name": bundle.get("baseRefName") or "",
        "head_ref_name": bundle.get("headRefName") or "",
        "head_sha": bundle.get("headRefOid") or None,
        "head_repo_owner_login": (bundle.get("headRepositoryOwner") or {}).get("login", ""),
        "head_repo_name": (bundle.get("headRepository") or {}).get("name", ""),
        "additions": int(bundle.get("additions", 0)),
        "deletions": int(bundle.get("deletions", 0)),
        "changed_files_count": int(bundle.get("changedFiles", 0)),
    }

    if pr is None:
        pr = PullRequest(repository=repo, number=number, **core_values)
        if not skip_watermark:
            pr.last_synced_at = timezone.now()
        pr.save()
        return PullRequestUpsertResult(pr=pr, created=True, updated_fields=tuple(core_values.keys()))

    # Newer-wins guard. Triggered only when callers explicitly pass an
    # ``if_newer_than`` cutoff (archive ingest does; live sync does not). When
    # the existing row already reflects a state newer than this snapshot, the
    # snapshot is strictly stale, so skip the whole core update rather than
    # regress any field (head_sha, gh_updated_at, additions/deletions, refs …).
    if if_newer_than is not None and pr.gh_updated_at is not None and pr.gh_updated_at > if_newer_than:
        return PullRequestUpsertResult(pr=pr, created=False, updated_fields=tuple())

    # Existing: update only changed core fields.
    # Note: last_synced_at is intentionally NOT advanced here. It is advanced only
    # after engagement fields (assignees, files, reviews, etc.) are also saved, in
    # sync_pull_request_bundle. Advancing it early would cause the skip check to
    # treat the PR as fully up-to-date even if engagement fields were never written
    # (e.g. due to a task failure between the two saves).
    updated, fields = update_if_changed(pr, core_values)
    return PullRequestUpsertResult(pr=pr, created=False, updated_fields=fields)
