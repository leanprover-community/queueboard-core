from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from django.utils import timezone

from analyzer.models import ReviewerOptOut
from core.models import Repository
from syncer.models import PullRequest, PRTimelineEvent, PRTimelineEventType


@dataclass(frozen=True)
class ReviewerOptOutBackfillResult:
    total_prs: int
    total_events: int
    opt_outs_created: int
    opt_outs_updated: int
    prs_updated: int

    def summary(self) -> str:
        return (
            "opt-outs backfilled: "
            f"prs={self.total_prs}, events={self.total_events}, "
            f"created={self.opt_outs_created}, updated={self.opt_outs_updated}, "
            f"prs_updated={self.prs_updated}"
        )


def _normalize_login(login: str | None) -> str:
    return (login or "").strip().lower()


def backfill_reviewer_opt_outs(
    *,
    repository: Repository | None = None,
    only_open: bool = True,
    require_complete: bool = True,
    cutoff_days: int | None = None,
    dry_run: bool = False,
) -> ReviewerOptOutBackfillResult:
    qs = PullRequest.objects.all()
    if repository is not None:
        qs = qs.filter(repository=repository)
    if only_open:
        qs = qs.filter(state="open")
    if require_complete:
        qs = qs.filter(timeline_backfill_done=True)

    prs = list(qs.only("id", "repository_id", "number", "last_assignment_event_at"))
    if not prs:
        return ReviewerOptOutBackfillResult(0, 0, 0, 0, 0)

    pr_map = {pr.id: pr for pr in prs}
    pr_ids = list(pr_map.keys())

    events_qs = PRTimelineEvent.objects.filter(
        pull_request_id__in=pr_ids,
        type__in=[PRTimelineEventType.ASSIGNED, PRTimelineEventType.UNASSIGNED],
        assignee_login__isnull=False,
    )
    if cutoff_days is not None:
        cutoff = timezone.now() - timezone.timedelta(days=int(cutoff_days))
        events_qs = events_qs.filter(occurred_at__gte=cutoff)

    events_qs = events_qs.order_by("pull_request_id", "assignee_login", "occurred_at", "id")

    latest: dict[tuple[int, str], tuple[timezone.datetime, str]] = {}
    max_seen_by_pr: dict[int, timezone.datetime | None] = defaultdict(lambda: None)
    total_events = 0
    for ev in events_qs.iterator():
        total_events += 1
        login = _normalize_login(ev.assignee_login)
        if not login:
            continue
        key = (ev.pull_request_id, login)
        latest[key] = (ev.occurred_at, ev.type)
        current_max = max_seen_by_pr[ev.pull_request_id]
        if current_max is None or ev.occurred_at > current_max:
            max_seen_by_pr[ev.pull_request_id] = ev.occurred_at

    created = 0
    updated = 0
    pr_updates = 0

    if not dry_run:
        for (pr_id, login), (occurred_at, ev_type) in latest.items():
            pr = pr_map[pr_id]
            is_unassign = ev_type == PRTimelineEventType.UNASSIGNED
            if is_unassign:
                obj, was_created = ReviewerOptOut.objects.update_or_create(
                    repository_id=pr.repository_id,
                    pr_number=pr.number,
                    reviewer_login=login,
                    defaults={
                        "active": True,
                        "opted_out_at": occurred_at,
                        "cleared_at": None,
                    },
                )
            else:
                obj = ReviewerOptOut.objects.filter(
                    repository_id=pr.repository_id,
                    pr_number=pr.number,
                    reviewer_login=login,
                ).first()
                if obj is None:
                    obj = ReviewerOptOut.objects.create(
                        repository_id=pr.repository_id,
                        pr_number=pr.number,
                        reviewer_login=login,
                        active=False,
                        opted_out_at=occurred_at,
                        cleared_at=occurred_at,
                    )
                    was_created = True
                else:
                    was_created = False
                    if obj.active or obj.cleared_at != occurred_at:
                        obj.active = False
                        obj.cleared_at = occurred_at
                        obj.save(update_fields=["active", "cleared_at"])
            created += 1 if was_created else 0
            updated += 0 if was_created else 1

        for pr_id, max_seen in max_seen_by_pr.items():
            if not max_seen:
                continue
            pr = pr_map[pr_id]
            if pr.last_assignment_event_at is None or max_seen > pr.last_assignment_event_at:
                PullRequest.objects.filter(id=pr.id).update(last_assignment_event_at=max_seen)
                pr_updates += 1

    return ReviewerOptOutBackfillResult(
        total_prs=len(prs),
        total_events=total_events,
        opt_outs_created=created,
        opt_outs_updated=updated,
        prs_updated=pr_updates,
    )
