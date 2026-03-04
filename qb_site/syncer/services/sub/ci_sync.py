from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List

from dateutil import parser as dtparser
from django.utils import timezone
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError

from analyzer.services.revisions import mark_pr_revision_dirty_if_earlier
from syncer.models.commit_check_run import CommitCheckRun
from syncer.models.commit_status_context import CommitStatusContext
from syncer.models.pull_request import PullRequest
from core.utils.db import update_if_changed, upsert_if_changed
import logging

log = logging.getLogger(__name__)


@dataclass
class CISyncResult:
    created: int = 0
    updated: int = 0
    deleted: int = 0


@dataclass
class _RevisionSignal:
    name_key: str
    row_ts: timezone.datetime
    signal_ts: timezone.datetime


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


def _upsert_commit_check_run(
    pr: PullRequest, values: dict[str, Any], gid: str, now: timezone.datetime
) -> tuple[bool, bool, tuple[str, ...]]:
    commit_values = {
        "repository": pr.repository,
        "github_node_id": gid,
        "head_sha": values["head_sha"],
        "name": values["name"],
        "status": values["status"],
        "conclusion": values["conclusion"],
        "details_url": values["details_url"],
        "external_id": values["external_id"],
        "gh_started_at": values["gh_started_at"],
        "gh_completed_at": values["gh_completed_at"],
    }
    try:
        commit_obj, was_created, was_updated, updated_fields = upsert_if_changed(
            CommitCheckRun,
            {"github_node_id": gid},
            commit_values,
        )
    except (IntegrityError, ObjectDoesNotExist):
        fallback_obj = None
        ext_id = values.get("external_id")
        if ext_id:
            fallback_obj = CommitCheckRun.objects.filter(
                repository=pr.repository,
                head_sha=values["head_sha"],
                name=values["name"],
                external_id=ext_id,
            ).first()
        if fallback_obj is None:
            log.warning(
                "CommitCheckRun dual-write conflict without fallback row for %s sha=%s gid=%s name=%s external_id=%s",
                pr.repository,
                values.get("head_sha"),
                gid,
                values.get("name"),
                ext_id,
            )
            return False, False, tuple()
        try:
            was_updated, updated_fields = update_if_changed(fallback_obj, commit_values)
            was_created = False
            commit_obj = fallback_obj
        except IntegrityError:
            log.warning(
                "CommitCheckRun fallback update conflict for %s sha=%s gid=%s name=%s external_id=%s",
                pr.repository,
                values.get("head_sha"),
                gid,
                values.get("name"),
                ext_id,
            )
            return False, False, tuple()
    CommitCheckRun.objects.filter(pk=commit_obj.pk).update(last_synced_at=now)
    return was_created, was_updated, updated_fields


def _upsert_commit_status_context(
    pr: PullRequest, values: dict[str, Any], gid: str, now: timezone.datetime
) -> tuple[bool, bool, tuple[str, ...]]:
    commit_values = {
        "repository": pr.repository,
        "github_node_id": gid,
        "head_sha": values["head_sha"],
        "name": values["name"],
        "state": values["state"],
        "target_url": values["target_url"],
        "description": values["description"],
        "gh_created_at": values["gh_created_at"],
    }
    try:
        commit_obj, was_created, was_updated, updated_fields = upsert_if_changed(
            CommitStatusContext,
            {"github_node_id": gid},
            commit_values,
        )
    except (IntegrityError, ObjectDoesNotExist):
        fallback_obj = CommitStatusContext.objects.filter(github_node_id=gid).first()
        if fallback_obj is None:
            log.warning(
                "CommitStatusContext dual-write conflict without fallback row for %s sha=%s gid=%s name=%s",
                pr.repository,
                values.get("head_sha"),
                gid,
                values.get("name"),
            )
            return False, False, tuple()
        try:
            was_updated, updated_fields = update_if_changed(fallback_obj, commit_values)
            was_created = False
            commit_obj = fallback_obj
        except IntegrityError:
            log.warning(
                "CommitStatusContext fallback update conflict for %s sha=%s gid=%s name=%s",
                pr.repository,
                values.get("head_sha"),
                gid,
                values.get("name"),
            )
            return False, False, tuple()
    CommitStatusContext.objects.filter(pk=commit_obj.pk).update(last_synced_at=now)
    return was_created, was_updated, updated_fields


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
    latest_by_name: dict[str, timezone.datetime] = {}
    revision_signals: list[_RevisionSignal] = []
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
            "head_sha": head_sha,
            "name": ctx.get("name") or "",
            "status": ctx.get("status") or "",
            "conclusion": ctx.get("conclusion"),
            "details_url": ctx.get("detailsUrl") or None,
            "external_id": ctx.get("externalId") or None,
            "gh_started_at": _parse_iso(ctx.get("startedAt")),
            "gh_completed_at": _parse_iso(ctx.get("completedAt")),
        }
        was_created = False
        was_updated = False
        updated_fields: tuple[str, ...] = tuple()
        commit_created, commit_updated, commit_updated_fields = _upsert_commit_check_run(pr, values, gid, now)
        was_created = commit_created
        was_updated = commit_updated
        updated_fields = commit_updated_fields

        created += 1 if was_created else 0
        updated += 1 if was_updated else 0

        # Only treat CI as a revision-boundary signal when evidence changed:
        # newly-seen rows or updates that affect head/timestamps.
        touches_revision_signal = was_created or bool({"head_sha", "gh_started_at", "gh_completed_at"} & set(updated_fields))
        ts = values["gh_completed_at"] or values["gh_started_at"]
        name_key = (values["name"] or "").strip().lower()
        row_ts = values["gh_completed_at"] or values["gh_started_at"]
        signal_ts = values["gh_started_at"] or values["gh_completed_at"]
        if touches_revision_signal and name_key and signal_ts is not None and row_ts is not None:
            revision_signals.append(_RevisionSignal(name_key=name_key, row_ts=row_ts, signal_ts=signal_ts))
        if name_key and ts is not None:
            current_latest = latest_by_name.get(name_key)
            if current_latest is None or ts > current_latest:
                latest_by_name[name_key] = ts

    # Only use revision signals from the newest snapshot per context name.
    # The GraphQL rollup can include older rows that we prune below; those
    # should not repeatedly dirty revision state.
    for signal in revision_signals:
        latest_for_name = latest_by_name.get(signal.name_key)
        if latest_for_name is None or signal.row_ts != latest_for_name:
            continue
        if earliest_ts is None or signal.signal_ts < earliest_ts:
            earliest_ts = signal.signal_ts

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
    latest_by_name: dict[str, timezone.datetime] = {}
    revision_signals: list[_RevisionSignal] = []
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
            "head_sha": head_sha,
            "name": ctx.get("context") or "",
            "state": ctx.get("state") or "",
            "target_url": ctx.get("targetUrl") or None,
            "description": ctx.get("description") or None,
            "gh_created_at": _parse_iso(ctx.get("createdAt")) or timezone.now(),
        }
        was_created = False
        was_updated = False
        updated_fields: tuple[str, ...] = tuple()
        commit_created, commit_updated, commit_updated_fields = _upsert_commit_status_context(pr, values, gid, now)
        was_created = commit_created
        was_updated = commit_updated
        updated_fields = commit_updated_fields

        created += 1 if was_created else 0
        updated += 1 if was_updated else 0

        ts = values["gh_created_at"]
        name_key = (values["name"] or "").strip().lower()
        touches_revision_signal = was_created or bool({"head_sha", "gh_created_at"} & set(updated_fields))
        if touches_revision_signal and name_key and ts is not None:
            revision_signals.append(_RevisionSignal(name_key=name_key, row_ts=ts, signal_ts=ts))
        if name_key and ts is not None:
            current_latest = latest_by_name.get(name_key)
            if current_latest is None or ts > current_latest:
                latest_by_name[name_key] = ts

    # Only use revision signals from the newest snapshot per context name.
    # The GraphQL rollup can include older rows that we prune below; those
    # should not repeatedly dirty revision state.
    for signal in revision_signals:
        latest_for_name = latest_by_name.get(signal.name_key)
        if latest_for_name is None or signal.signal_ts != latest_for_name:
            continue
        if earliest_ts is None or signal.signal_ts < earliest_ts:
            earliest_ts = signal.signal_ts

    if earliest_ts:
        mark_pr_revision_dirty_if_earlier(pr, earliest_ts)

    return CISyncResult(created=created, updated=updated, deleted=0)
