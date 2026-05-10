from __future__ import annotations

from datetime import datetime
from typing import Iterable

from analyzer.models import PRQueueWindowBuildState, QueueRuleSet
from syncer.models import PullRequest


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
