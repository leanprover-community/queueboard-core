"""Per-PR archive backfill ingest service (design doc 043).

Reads a single legacy ``pr_info.json`` payload (the GraphQL ``pullRequest``
node from one of the queueboard-archive repos) and persists it into the
live syncer tables, using the archive-mode flags introduced on the
sub-syncs in this same commit. Wrapped in a per-PR ``transaction.atomic()``
block so a parse error or constraint failure mid-ingest does not leave
half-imported rows.

Provenance: rows the call inserts get ``archive_imported_at`` stamped
to the call's start time. Rows that pre-existed are not stamped.
"""

from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List

import requests
from dateutil import parser as dtparser
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core.models.repository import Repository
from syncer.models import (
    CommitCheckRun,
    CommitStatusContext,
    PRTimelineEvent,
    PullRequest,
)
from syncer.services.sub.ci_sync import sync_check_runs, sync_status_contexts
from syncer.services.sub.labels_sync import sync_pr_labels
from syncer.services.sub.pull_request_sync import upsert_pull_request
from syncer.services.sub.timeline_sync import sync_timeline_events

log = logging.getLogger(__name__)


@dataclass
class ArchiveImportResult:
    pr: PullRequest | None = None
    pr_created: bool = False
    timeline_created: int = 0
    timeline_updated: int = 0
    check_runs_created: int = 0
    check_runs_updated: int = 0
    status_contexts_created: int = 0
    status_contexts_updated: int = 0
    labels_attached: int = 0
    head_shas_touched: list[str] = field(default_factory=list)


class ArchivePayloadError(ValueError):
    """Raised for legacy payloads we cannot ingest (missing PR shape, bad number, etc.)."""


def unwrap_pr_info_payload(data: Any) -> Dict[str, Any]:
    """Return the unwrapped ``pullRequest`` object regardless of file shape.

    Legacy ``pr_info.json`` files have varied across the lifetime of the
    archive. Some carry the full GraphQL response wrapper
    (``{"data": {"repository": {"pullRequest": {...}}}}``), some carry the
    unwrapped node directly. Both shapes are handled here so the rest of
    the importer can work against a uniform payload.
    """
    if not isinstance(data, dict):
        raise ArchivePayloadError(f"pr_info payload must be a JSON object, got {type(data).__name__}")
    candidate = data
    if "data" in candidate and isinstance(candidate["data"], dict):
        repo = candidate["data"].get("repository")
        if isinstance(repo, dict) and isinstance(repo.get("pullRequest"), dict):
            return repo["pullRequest"]
    if "pullRequest" in candidate and isinstance(candidate["pullRequest"], dict):
        return candidate["pullRequest"]
    if isinstance(candidate.get("number"), int):
        return candidate
    raise ArchivePayloadError("pr_info payload does not contain a recognizable pullRequest node")


def fetch_pr_info(archive_name: str, pr_number: int, *, archive_owner: str = "leanprover-community") -> bytes:
    """HTTP GET ``data/<N>/pr_info.json`` from raw.githubusercontent.com.

    Uses ``ARCHIVE_IMPORT_RAW_BASE_URL`` and
    ``ARCHIVE_IMPORT_FETCH_TIMEOUT_SECONDS`` from settings. Returns the
    raw bytes; the caller is responsible for JSON parsing and unwrapping.
    Caller also classifies HTTP errors (404 → permanent, 5xx/timeout →
    transient).
    """
    base = getattr(settings, "ARCHIVE_IMPORT_RAW_BASE_URL", "https://raw.githubusercontent.com").rstrip("/")
    timeout = int(getattr(settings, "ARCHIVE_IMPORT_FETCH_TIMEOUT_SECONDS", 30))
    url = f"{base}/{archive_owner}/{archive_name}/master/data/{pr_number}/pr_info.json"
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content


def _split_contexts(
    contexts: Iterable[Any],
    *,
    commit_committed_at_iso: str | None,
    archive_timestamp_iso: str | None,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split a ``statusCheckRollup.contexts.nodes`` list into CheckRun / StatusContext.

    The legacy ``pr_info.graphql`` fragment requests no ``startedAt`` /
    ``completedAt`` for ``CheckRun`` and no ``createdAt`` for
    ``StatusContext`` (see doc 043 §"Legacy archive payload schema
    deficiency"). Inserting NULLs is fine for the model schema, but the
    analyzer's ``_latest_ci_statuses_for_fragment`` filter silently
    excludes rows where both timestamps are NULL — which would defeat the
    importer's orphan-SHA recovery use case, since the analyzer would
    never count those rescued CheckRuns.

    Synthesis source preference, per-row:
      1. ``commit.committedDate`` — best proxy. The commit's authored time
         is always ≤ any analyzer evaluation time within that SHA's
         revision window, so the filter ``gh_completed_at <= at`` will
         match. Also preserves realistic per-commit ordering within a PR.
         (CI runs typically complete slightly after committedDate; the
         bias is small relative to typical revision-window durations.)
      2. ``archive_timestamp`` — per-PR ``timestamp.txt`` scrape time.
         Used only when committedDate is missing. Sorts to "around the
         scrape time" — later than reality, so the analyzer's filter
         may still exclude these rows for past ``at`` queries. Acceptable
         as a NULL-avoidance fallback for malformed payloads.
    """
    synth_ts = commit_committed_at_iso or archive_timestamp_iso
    check_runs: list[Dict[str, Any]] = []
    status_contexts: list[Dict[str, Any]] = []
    for ctx in contexts or []:
        if not isinstance(ctx, dict):
            continue
        typename = ctx.get("__typename")
        if typename == "CheckRun":
            if synth_ts:
                overrides: dict[str, Any] = {}
                if not ctx.get("startedAt"):
                    overrides["startedAt"] = synth_ts
                if not ctx.get("completedAt"):
                    overrides["completedAt"] = synth_ts
                if overrides:
                    ctx = {**ctx, **overrides}
            check_runs.append(ctx)
        elif typename == "StatusContext":
            if not ctx.get("createdAt") and synth_ts:
                ctx = {**ctx, "createdAt": synth_ts}
            status_contexts.append(ctx)
    return check_runs, status_contexts


def _label_names_from_payload(payload: Dict[str, Any]) -> list[str]:
    labels = (payload.get("labels") or {}).get("nodes") or []
    out: list[str] = []
    for node in labels:
        if isinstance(node, dict):
            name = node.get("name")
            if isinstance(name, str) and name:
                out.append(name)
    return out


def import_pr_info_payload(
    repository: Repository,
    payload: Dict[str, Any],
    *,
    archive_name: str,
    archive_timestamp: _dt.datetime | None,
) -> ArchiveImportResult:
    """Persist a single archive ``pullRequest`` payload into the live tables.

    Wrapped in ``transaction.atomic()`` so a constraint failure mid-ingest
    does not leave a half-imported PR. Sub-syncs run with their archive-mode
    flags so the ingest:
      - Does not advance ``last_synced_at`` (archive watermark suppression).
      - Honors a newer-wins guard on the PR core fields.
      - Does not detach labels added live since the archive snapshot.
      - Skips dismissed-review parent synthesis (legacy fragment lacks
        ``previousReviewState``).
      - Strips NULL CI fields on update so live's non-null ``external_id`` /
        timestamps are not downgraded.
    Provenance: rows the call inserts get ``archive_imported_at`` set to
    the call's start time.
    """
    result = ArchiveImportResult()
    archive_ts_iso = archive_timestamp.isoformat() if archive_timestamp else None
    snapshot_updated_at_iso = payload.get("updatedAt")
    snapshot_updated_at = _parse_iso(snapshot_updated_at_iso) if isinstance(snapshot_updated_at_iso, str) else None

    with transaction.atomic():
        now = timezone.now()

        upsert = upsert_pull_request(
            payload,
            repository,
            if_newer_than=snapshot_updated_at,
            skip_watermark=True,
        )
        pr = upsert.pr
        result.pr = pr
        result.pr_created = upsert.created

        # Labels — additive only.
        label_res = sync_pr_labels(pr, _label_names_from_payload(payload), additive_only=True)
        result.labels_attached = label_res.created

        # Timeline events — archive-mode skips dismissed-review parent synthesis.
        timeline_events = (payload.get("timelineItems") or {}).get("nodes") or []
        tl_res = sync_timeline_events(pr, timeline_events, archive_mode=True)
        result.timeline_created = tl_res.created
        result.timeline_updated = tl_res.updated

        # Per-commit CI: walk commits.nodes[].commit.statusCheckRollup.contexts.nodes
        commits = (payload.get("commits") or {}).get("nodes") or []
        head_shas_touched: list[str] = []
        for node in commits:
            if not isinstance(node, dict):
                continue
            commit = node.get("commit") or {}
            head_sha = commit.get("oid")
            if not isinstance(head_sha, str) or not head_sha:
                continue
            rollup = commit.get("statusCheckRollup") or {}
            contexts_nodes = ((rollup.get("contexts") or {}).get("nodes")) or []
            commit_committed_at_iso = commit.get("committedDate")
            check_runs, status_contexts = _split_contexts(
                contexts_nodes,
                commit_committed_at_iso=commit_committed_at_iso,
                archive_timestamp_iso=archive_ts_iso,
            )
            if check_runs:
                cr_res = sync_check_runs(pr, check_runs, head_sha, archive_mode=True)
                result.check_runs_created += cr_res.created
                result.check_runs_updated += cr_res.updated
            if status_contexts:
                sc_res = sync_status_contexts(pr, status_contexts, head_sha, archive_mode=True)
                result.status_contexts_created += sc_res.created
                result.status_contexts_updated += sc_res.updated
            head_shas_touched.append(head_sha)
        result.head_shas_touched = head_shas_touched

        _stamp_archive_provenance(
            pr=pr,
            pr_created=upsert.created,
            head_shas=head_shas_touched,
            now=now,
        )

    return result


def _stamp_archive_provenance(
    *,
    pr: PullRequest,
    pr_created: bool,
    head_shas: list[str],
    now: _dt.datetime,
) -> None:
    """Set ``archive_imported_at = now`` on rows the importer just created.

    Filter is narrow: by PR (timeline) or by repo+head-sha (CI), plus
    ``archive_imported_at IS NULL`` and ``created_at >= now``. Inside the
    importer's transaction, this matches rows we wrote in this call —
    plus, theoretically, rows another writer committed in the same
    interval, but the live syncer does not race the archive importer on
    these specific (PR, sha) tuples in practice.
    """
    if pr_created and pr.archive_imported_at is None:
        PullRequest.objects.filter(pk=pr.pk).update(archive_imported_at=now)
        # Reflect on the in-memory instance so callers' assertions see it.
        pr.archive_imported_at = now
    PRTimelineEvent.objects.filter(
        pull_request=pr,
        archive_imported_at__isnull=True,
        created_at__gte=now,
    ).update(archive_imported_at=now)
    if head_shas:
        CommitCheckRun.objects.filter(
            repository=pr.repository,
            head_sha__in=head_shas,
            archive_imported_at__isnull=True,
            created_at__gte=now,
        ).update(archive_imported_at=now)
        CommitStatusContext.objects.filter(
            repository=pr.repository,
            head_sha__in=head_shas,
            archive_imported_at__isnull=True,
            created_at__gte=now,
        ).update(archive_imported_at=now)


def _parse_iso(val: str | None) -> _dt.datetime | None:
    if not val:
        return None
    dt = dtparser.isoparse(val)
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    return dt
