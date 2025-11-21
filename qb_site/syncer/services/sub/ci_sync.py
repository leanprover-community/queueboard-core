from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List

from dateutil import parser as dtparser
from django.utils import timezone
from django.conf import settings

from analyzer.services.revisions import mark_pr_revision_dirty_if_earlier
from syncer.models.check_run import CheckRun
from syncer.models.pull_request import PullRequest
from syncer.models.status_context import StatusContext
from core.utils.db import upsert_if_changed
import logging

log = logging.getLogger(__name__)


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


def _parse_allowlist(val: Any) -> List[str]:
    if not val:
        return []
    s = str(val)
    return [tok.strip().lower() for tok in s.split(",") if tok.strip()]


def _effective_allowlist_for_checkruns(pr: PullRequest) -> List[str]:
    """Return the allowlist patterns for CheckRun contexts for a PR's repository."""
    repo_patterns = getattr(pr.repository, "ci_tracked_checkrun_names", None) or []
    if repo_patterns:
        return [str(p).strip().lower() for p in repo_patterns if str(p).strip()]
    mode = getattr(settings, "SYNCER_CI_FILTER_MODE", "all")
    if mode == "allowlist":
        return _parse_allowlist(getattr(settings, "SYNCER_CI_ALLOW_CHECKRUN_NAMES", ""))
    return []


def _effective_allowlist_for_status(pr: PullRequest) -> List[str]:
    """Return the allowlist patterns for StatusContext contexts for a PR's repository."""
    repo_patterns = getattr(pr.repository, "ci_tracked_status_names", None) or []
    if repo_patterns:
        return [str(p).strip().lower() for p in repo_patterns if str(p).strip()]
    mode = getattr(settings, "SYNCER_CI_FILTER_MODE", "all")
    if mode == "allowlist":
        return _parse_allowlist(getattr(settings, "SYNCER_CI_ALLOW_STATUS_NAMES", ""))
    return []


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
    allow = _effective_allowlist_for_checkruns(pr)
    if allow:
        log.debug("CI sync: using CheckRun allowlist for %s (patterns=%s)", pr.repository, allow)
    now = timezone.now()
    earliest_ts = None
    for ctx in contexts:
        if not isinstance(ctx, dict):
            continue
        gid = ctx.get("id")
        if not gid:
            continue
        # Optional allow-list filter by name (case-insensitive substring)
        if allow:
            nm = (ctx.get("name") or "").lower()
            if not any(pat in nm for pat in allow):
                log.debug("CI sync: skipping CheckRun %s due to allowlist (pat=%s)", nm, allow)
                continue
        if (ctx.get("conclusion") or "").upper() == "SKIPPED":
            log.debug("CI sync: skipping CheckRun %s due to SKIPPED conclusion", ctx.get("name"))
            continue
        values = {
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
        obj, was_created, was_updated, _ = upsert_if_changed(
            CheckRun,
            {"github_node_id": gid},
            values,
        )
        # Always record when we last heard about this CheckRun from GitHub,
        # even if the status snapshot itself did not change.
        CheckRun.objects.filter(pk=obj.pk).update(last_synced_at=now)
        created += 1 if was_created else 0
        updated += 1 if was_updated else 0

        # Track earliest timestamp seen in this batch to flag potential revision dirtiness.
        for ts in (values["gh_started_at"], values["gh_completed_at"]):
            if ts is None:
                continue
            if earliest_ts is None or ts < earliest_ts:
                earliest_ts = ts

    if earliest_ts:
        mark_pr_revision_dirty_if_earlier(pr, earliest_ts)

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
    allow = _effective_allowlist_for_status(pr)
    if allow:
        log.debug("CI sync: using StatusContext allowlist for %s (patterns=%s)", pr.repository, allow)
    now = timezone.now()
    earliest_ts = None
    for ctx in contexts:
        if not isinstance(ctx, dict):
            continue
        gid = ctx.get("id")
        if not gid:
            continue
        # Optional allow-list filter by context name (case-insensitive substring)
        if allow:
            nm = (ctx.get("context") or "").lower()
            if not any(pat in nm for pat in allow):
                log.debug("CI sync: skipping StatusContext %s due to allowlist (pat=%s)", nm, allow)
                continue
        values = {
            "pull_request": pr,
            "head_sha": head_sha,
            "name": ctx.get("context") or "",
            "state": ctx.get("state") or "",
            "target_url": ctx.get("targetUrl") or None,
            "description": ctx.get("description") or None,
            "gh_created_at": _parse_iso(ctx.get("createdAt")) or timezone.now(),
        }
        obj, was_created, was_updated, _ = upsert_if_changed(
            StatusContext,
            {"github_node_id": gid},
            values,
        )
        StatusContext.objects.filter(pk=obj.pk).update(last_synced_at=now)
        created += 1 if was_created else 0
        updated += 1 if was_updated else 0

        ts = values["gh_created_at"]
        if ts is not None and (earliest_ts is None or ts < earliest_ts):
            earliest_ts = ts

    if earliest_ts:
        mark_pr_revision_dirty_if_earlier(pr, earliest_ts)

    return CISyncResult(created=created, updated=updated, deleted=0)
