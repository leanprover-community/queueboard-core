from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable

from dateutil import parser as dtparser
from django.utils import timezone

from syncer.models.check_run import CheckRun
from syncer.models.pull_request import PullRequest
from syncer.models.status_context import StatusContext


@dataclass
class CISyncResult:
    created: int = 0
    updated: int = 0
    deleted: int = 0


def _parse_iso(val: str | None):
    if not val:
        return None
    dt = dtparser.isoparse(val)
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    return dt


def sync_check_runs(pr: PullRequest, contexts: Iterable[Dict[str, Any]], head_sha: str) -> CISyncResult:
    """Upsert snapshot CheckRun rows from a commit's status.contexts entries.

    Inputs are the subset of contexts where __typename == "CheckRun" with keys:
      {"id": str, "name": str, "status": str, "conclusion": str | None,
       "startedAt": str | None, "completedAt": str | None, "detailsUrl": str | None,
       "externalId": str | None}

    The head_sha for these contexts must be passed alongside and stored on each CheckRun row.
    """
    created = 0
    updated = 0
    for ctx in contexts:
        if not isinstance(ctx, dict):
            continue
        gid = ctx.get("id")
        if not gid:
            continue
        defaults = {
            "pull_request": pr,
            "head_sha": head_sha,
            "name": ctx.get("name") or "",
            "status": ctx.get("status") or "",
            "conclusion": ctx.get("conclusion"),
            "details_url": ctx.get("detailsUrl") or None,
            "external_id": ctx.get("externalId") or None,
            "gh_started_at": _parse_iso(ctx.get("startedAt")),
            "gh_completed_at": _parse_iso(ctx.get("completedAt")),
        }
        obj, was_created = CheckRun.objects.update_or_create(github_node_id=gid, defaults=defaults)
        created += 1 if was_created else 0
        updated += 0 if was_created else 1
    return CISyncResult(created=created, updated=updated, deleted=0)


def sync_status_contexts(pr: PullRequest, contexts: Iterable[Dict[str, Any]], head_sha: str) -> CISyncResult:
    """Upsert snapshot StatusContext rows from a commit's status.contexts entries.

    Inputs are the subset of contexts where __typename == "StatusContext" with keys:
      {"id": str, "context": str, "state": str, "targetUrl": str | None,
       "description": str | None, "createdAt": str}

    The head_sha for these contexts must be passed alongside and stored on each row.
    """
    created = 0
    updated = 0
    for ctx in contexts:
        if not isinstance(ctx, dict):
            continue
        gid = ctx.get("id")
        if not gid:
            continue
        defaults = {
            "pull_request": pr,
            "head_sha": head_sha,
            "name": ctx.get("context") or "",
            "state": ctx.get("state") or "",
            "target_url": ctx.get("targetUrl") or None,
            "description": ctx.get("description") or None,
            "gh_created_at": _parse_iso(ctx.get("createdAt")) or timezone.now(),
        }
        obj, was_created = StatusContext.objects.update_or_create(github_node_id=gid, defaults=defaults)
        created += 1 if was_created else 0
        updated += 0 if was_created else 1
    return CISyncResult(created=created, updated=updated, deleted=0)
