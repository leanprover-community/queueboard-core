from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from django.db import transaction

from analyzer.models import PRQueueWindowBuildState, QueueRuleSet
from core.models import Repository
from syncer.models import PullRequest


@dataclass(frozen=True)
class QueueWindowBuildStateBackfillResult:
    repo: str
    prs_considered: int
    rows_created: int
    rows_updated: int
    dry_run: bool


def record_queue_window_build_states(
    *,
    pr: PullRequest,
    rule_sets: Iterable[QueueRuleSet],
    per_ruleset: dict[int, dict[str, object]] | dict[object, object] | None,
    revision_version: int,
    built_at: datetime,
) -> None:
    """Upsert queue-window build-state metadata for the given PR/ruleset pairs."""
    rule_sets = list(rule_sets)
    if not rule_sets:
        return

    rule_set_ids = [int(rs.id) for rs in rule_sets]
    existing = {
        row.rule_set_id: row
        for row in PRQueueWindowBuildState.objects.filter(
            pull_request=pr,
            rule_set_id__in=rule_set_ids,
        )
    }

    to_create: list[PRQueueWindowBuildState] = []
    to_update: list[PRQueueWindowBuildState] = []
    per_ruleset_map = per_ruleset or {}

    for rule_set in rule_sets:
        rule_set_id = int(rule_set.id)
        raw = per_ruleset_map.get(rule_set_id, {})
        status: str | None = None
        reason: str | None = None
        if isinstance(raw, dict):
            raw_status = raw.get("status")
            raw_reason = raw.get("reason")
            status = str(raw_status)[:32] if raw_status is not None else None
            reason = str(raw_reason)[:128] if raw_reason is not None else None

        row = existing.get(rule_set_id)
        if row is None:
            to_create.append(
                PRQueueWindowBuildState(
                    pull_request=pr,
                    rule_set=rule_set,
                    revision_version_built=revision_version,
                    windows_built_at=built_at,
                    last_status=status,
                    last_reason=reason,
                )
            )
            continue

        changed = False
        if row.revision_version_built != revision_version:
            row.revision_version_built = revision_version
            changed = True
        if row.windows_built_at != built_at:
            row.windows_built_at = built_at
            changed = True
        if row.last_status != status:
            row.last_status = status
            changed = True
        if row.last_reason != reason:
            row.last_reason = reason
            changed = True
        if changed:
            to_update.append(row)

    if to_create:
        PRQueueWindowBuildState.objects.bulk_create(to_create, batch_size=200)
    if to_update:
        PRQueueWindowBuildState.objects.bulk_update(
            to_update,
            ["revision_version_built", "windows_built_at", "last_status", "last_reason", "updated_at"],
            batch_size=200,
        )


def backfill_queue_window_build_states_for_repo(
    *,
    repository: Repository,
    pr_numbers: list[int] | None = None,
    dry_run: bool = True,
) -> QueueWindowBuildStateBackfillResult:
    """Backfill per-ruleset queue-window build state from legacy PR-level fields."""
    rulesets = list(QueueRuleSet.objects.filter(repository=repository, is_active=True).order_by("id"))
    if not rulesets:
        return QueueWindowBuildStateBackfillResult(
            repo=f"{repository.owner}/{repository.name}",
            prs_considered=0,
            rows_created=0,
            rows_updated=0,
            dry_run=bool(dry_run),
        )

    prs = PullRequest.objects.filter(repository=repository, timeline_backfill_done=True).select_related("revision_build_state")
    if pr_numbers:
        prs = prs.filter(number__in=pr_numbers)
    prs = list(prs.only("id", "number", "timeline_backfill_done", "revision_build_state__revision_version"))
    if not prs:
        return QueueWindowBuildStateBackfillResult(
            repo=f"{repository.owner}/{repository.name}",
            prs_considered=0,
            rows_created=0,
            rows_updated=0,
            dry_run=bool(dry_run),
        )

    pr_ids = [int(pr.id) for pr in prs]
    rule_set_ids = [int(rs.id) for rs in rulesets]
    existing = {
        (int(row.pull_request_id), int(row.rule_set_id)): row
        for row in PRQueueWindowBuildState.objects.filter(pull_request_id__in=pr_ids, rule_set_id__in=rule_set_ids)
    }

    to_create: list[PRQueueWindowBuildState] = []
    to_update: list[PRQueueWindowBuildState] = []
    for pr in prs:
        state = getattr(pr, "revision_build_state", None)
        revision_version_built = None
        windows_built_at = None
        if state is not None:
            revision_version_built = state.windows_built_revision_version
            windows_built_at = state.windows_built_at

        for rs in rulesets:
            key = (int(pr.id), int(rs.id))
            row = existing.get(key)
            if row is None:
                to_create.append(
                    PRQueueWindowBuildState(
                        pull_request_id=int(pr.id),
                        rule_set_id=int(rs.id),
                        revision_version_built=revision_version_built,
                        windows_built_at=windows_built_at,
                        last_status="backfilled",
                        last_reason="legacy_pr_build_state",
                    )
                )
                continue

            changed = False
            if row.revision_version_built != revision_version_built:
                row.revision_version_built = revision_version_built
                changed = True
            if row.windows_built_at != windows_built_at:
                row.windows_built_at = windows_built_at
                changed = True
            if row.last_status != "backfilled":
                row.last_status = "backfilled"
                changed = True
            if row.last_reason != "legacy_pr_build_state":
                row.last_reason = "legacy_pr_build_state"
                changed = True
            if changed:
                to_update.append(row)

    created = len(to_create)
    updated = len(to_update)
    if not dry_run:
        with transaction.atomic():
            if to_create:
                PRQueueWindowBuildState.objects.bulk_create(to_create, batch_size=200)
            if to_update:
                PRQueueWindowBuildState.objects.bulk_update(
                    to_update,
                    ["revision_version_built", "windows_built_at", "last_status", "last_reason", "updated_at"],
                    batch_size=200,
                )

    return QueueWindowBuildStateBackfillResult(
        repo=f"{repository.owner}/{repository.name}",
        prs_considered=len(prs),
        rows_created=created,
        rows_updated=updated,
        dry_run=bool(dry_run),
    )
