from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable

from dateutil import parser as dtparser
from django.utils import timezone

from analyzer.services.revisions import mark_pr_revision_dirty_if_earlier
from syncer.models.pr_timeline_event import PRTimelineEvent, PRTimelineEventType
from syncer.models.pull_request import PullRequest


@dataclass
class TimelineSyncResult:
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


def sync_timeline_events(pr: PullRequest, events: Iterable[Dict[str, Any]]) -> TimelineSyncResult:
    """Insert key timeline events for a PR using GraphQL ids (idempotent).

    Expected event items from the bundle's timelineItems.nodes include:
      - {"__typename": "LabeledEvent", "id": str, "createdAt": str, "label": {"name": str}}
      - {"__typename": "UnlabeledEvent", "id": str, "createdAt": str, "label": {"name": str}}
      - {"__typename": "ReadyForReviewEvent" | "ConvertToDraftEvent" | "ReopenedEvent" | "ClosedEvent",
         "id": str, "createdAt": str}
      - {"__typename": "HeadRefForcePushedEvent", "id": str, "createdAt": str,
         "beforeCommit": {"oid": str}, "afterCommit": {"oid": str}}

    This function should:
      - Map __typename → PRTimelineEventType
      - Use bulk_create(ignore_conflicts=True) keyed by github_node_id
      - Store label_name for label events
    """
    created = 0
    reset_commits_backfill = False
    earliest_new_ts = None
    type_map = {
        "LabeledEvent": PRTimelineEventType.LABELED,
        "UnlabeledEvent": PRTimelineEventType.UNLABELED,
        "AssignedEvent": PRTimelineEventType.ASSIGNED,
        "UnassignedEvent": PRTimelineEventType.UNASSIGNED,
        "ReadyForReviewEvent": PRTimelineEventType.READY_FOR_REVIEW,
        "ConvertToDraftEvent": PRTimelineEventType.CONVERT_TO_DRAFT,
        "ReopenedEvent": PRTimelineEventType.REOPENED,
        "ClosedEvent": PRTimelineEventType.CLOSED,
        "HeadRefForcePushedEvent": PRTimelineEventType.HEAD_FORCE_PUSHED,
    }
    for ev in events:
        if not isinstance(ev, dict):
            continue
        typename = ev.get("__typename")
        gid = ev.get("id")
        occurred_at = _parse_iso(ev.get("createdAt"))
        if not gid or not occurred_at:
            continue
        label_name = None
        if typename in ("LabeledEvent", "UnlabeledEvent"):
            label = ev.get("label") or {}
            label_name = label.get("name")
        assignee_login = None
        if typename in ("AssignedEvent", "UnassignedEvent"):
            assignee = ev.get("assignee") or {}
            assignee_login = assignee.get("login")
        actor = ev.get("actor") or {}
        actor_login = actor.get("login")
        before_sha = None
        after_sha = None
        if typename == "HeadRefForcePushedEvent":
            bc = ev.get("beforeCommit") or {}
            ac = ev.get("afterCommit") or {}
            before_sha = bc.get("oid")
            after_sha = ac.get("oid")
        ev_type = type_map.get(typename)
        if ev_type is None:
            continue
        obj, was_created = PRTimelineEvent.objects.get_or_create(
            pull_request=pr,
            github_node_id=gid,
            defaults={
                "type": ev_type,
                "occurred_at": occurred_at,
                "label_name": label_name,
                "assignee_login": assignee_login,
                "actor_login": actor_login,
                "before_sha": before_sha,
                "after_sha": after_sha,
            },
        )
        if was_created:
            created += 1
            if earliest_new_ts is None or occurred_at < earliest_new_ts:
                earliest_new_ts = occurred_at
            if ev_type == PRTimelineEventType.HEAD_FORCE_PUSHED:
                reset_commits_backfill = True

    if reset_commits_backfill:
        pr.commits_backfill_done = False
        pr.commits_backfill_cursor = None
        pr.commits_earliest_synced_at = None
        pr.save(update_fields=["commits_backfill_done", "commits_backfill_cursor", "commits_earliest_synced_at"])

    if earliest_new_ts:
        mark_pr_revision_dirty_if_earlier(pr, earliest_new_ts)

    return TimelineSyncResult(created=created, updated=0, deleted=0)
