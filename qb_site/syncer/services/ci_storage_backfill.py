from __future__ import annotations

from dataclasses import dataclass

from django.db import IntegrityError

from core.utils.db import upsert_if_changed
from syncer.models import CheckRun, CommitCheckRun, CommitStatusContext, StatusContext


@dataclass
class BackfillModelStats:
    scanned: int = 0
    inserted: int = 0
    updated: int = 0
    skipped_duplicate: int = 0
    skipped_invalid: int = 0
    skipped_conflict: int = 0
    next_start_id: int = 0


@dataclass
class BackfillStats:
    check_runs: BackfillModelStats
    status_contexts: BackfillModelStats

    def to_dict(self) -> dict[str, dict[str, int]]:
        return {
            "check_runs": self.check_runs.__dict__,
            "status_contexts": self.status_contexts.__dict__,
        }


def _process_check_run_row(row: CheckRun, stats: BackfillModelStats) -> None:
    stats.scanned += 1
    stats.next_start_id = row.id
    if not row.github_node_id or not row.head_sha:
        stats.skipped_invalid += 1
        return
    values = {
        "repository": row.pull_request.repository,
        "head_sha": row.head_sha,
        "name": row.name,
        "status": row.status,
        "conclusion": row.conclusion,
        "details_url": row.details_url,
        "external_id": row.external_id,
        "gh_started_at": row.gh_started_at,
        "gh_completed_at": row.gh_completed_at,
        "last_synced_at": row.last_synced_at,
    }
    try:
        _, created, updated, _ = upsert_if_changed(CommitCheckRun, {"github_node_id": row.github_node_id}, values)
    except IntegrityError:
        stats.skipped_conflict += 1
        return
    if created:
        stats.inserted += 1
    elif updated:
        stats.updated += 1
    else:
        stats.skipped_duplicate += 1


def _process_status_context_row(row: StatusContext, stats: BackfillModelStats) -> None:
    stats.scanned += 1
    stats.next_start_id = row.id
    lookup: dict[str, str | int] = {}
    if row.github_node_id:
        lookup = {"github_node_id": row.github_node_id}
    elif row.rest_id is not None:
        lookup = {"rest_id": row.rest_id}
    else:
        stats.skipped_invalid += 1
        return
    if not row.head_sha:
        stats.skipped_invalid += 1
        return
    values = {
        "repository": row.pull_request.repository,
        "github_node_id": row.github_node_id,
        "rest_id": row.rest_id,
        "head_sha": row.head_sha,
        "name": row.name,
        "state": row.state,
        "target_url": row.target_url,
        "description": row.description,
        "gh_created_at": row.gh_created_at,
        "last_synced_at": row.last_synced_at,
    }
    try:
        _, created, updated, _ = upsert_if_changed(CommitStatusContext, lookup, values)
    except IntegrityError:
        stats.skipped_conflict += 1
        return
    if created:
        stats.inserted += 1
    elif updated:
        stats.updated += 1
    else:
        stats.skipped_duplicate += 1


def backfill_commit_ci_rows(
    *,
    checkrun_start_id: int = 0,
    status_start_id: int = 0,
    batch_size: int = 1000,
    max_checkruns: int | None = None,
    max_status_contexts: int | None = None,
    repo_id: int | None = None,
) -> BackfillStats:
    """Backfill commit-scoped CI rows from legacy PR-scoped rows.

    Processing is idempotent and resumable by providing ``*_start_id`` values from a
    previous run's ``next_start_id`` fields.
    """
    cr_stats = BackfillModelStats(next_start_id=max(0, int(checkrun_start_id)))
    sc_stats = BackfillModelStats(next_start_id=max(0, int(status_start_id)))
    batch = max(1, int(batch_size))

    remaining_cr = max_checkruns if max_checkruns is None else max(0, int(max_checkruns))
    while remaining_cr is None or remaining_cr > 0:
        limit = batch if remaining_cr is None else min(batch, remaining_cr)
        qs = CheckRun.objects.select_related("pull_request__repository").filter(id__gt=cr_stats.next_start_id)
        if repo_id is not None:
            qs = qs.filter(pull_request__repository_id=repo_id)
        rows = list(qs.order_by("id")[:limit])
        if not rows:
            break
        for row in rows:
            _process_check_run_row(row, cr_stats)
        if remaining_cr is not None:
            remaining_cr -= len(rows)

    remaining_sc = max_status_contexts if max_status_contexts is None else max(0, int(max_status_contexts))
    while remaining_sc is None or remaining_sc > 0:
        limit = batch if remaining_sc is None else min(batch, remaining_sc)
        qs = StatusContext.objects.select_related("pull_request__repository").filter(id__gt=sc_stats.next_start_id)
        if repo_id is not None:
            qs = qs.filter(pull_request__repository_id=repo_id)
        rows = list(qs.order_by("id")[:limit])
        if not rows:
            break
        for row in rows:
            _process_status_context_row(row, sc_stats)
        if remaining_sc is not None:
            remaining_sc -= len(rows)

    return BackfillStats(check_runs=cr_stats, status_contexts=sc_stats)
