from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, List, Optional, Tuple

from django.utils import timezone

from core.models import Repository
from analyzer.models import PRQueueWindow, QueueRuleSet
from analyzer.services.queue_rules import QueueRules, load_rules_for_repo, rules_for_rule_set
from syncer.models import PullRequest, PRTimelineEvent, PRTimelineEventType


QueueWindow = Tuple[datetime, datetime]


@dataclass
class QueueSummary:
    """Summary of queue time for a PR."""

    pr: PullRequest
    as_of: datetime
    total_seconds: float


@dataclass
class QueueWindowRebuildResult:
    created: int
    updated: int
    deleted: int


def _normalize_label(name: str | None) -> str:
    return (name or "").strip().lower()


@dataclass
class _State:
    labels: set[str]
    is_draft: bool
    is_open: bool
    ci_ok: Optional[bool]


def _initial_state(pr: PullRequest) -> _State:
    # Start with empty label set; label history is derived solely from timeline events.
    # Treat the PR as open from creation until closed/merged (or until explicit
    # CLOSED/REOPENED timeline events say otherwise). This avoids relying on the
    # current ``state`` field when reconstructing historical windows.
    return _State(labels=set(), is_draft=bool(pr.is_draft), is_open=True, ci_ok=None)


def _iter_state_events(pr: PullRequest):
    qs = PRTimelineEvent.objects.filter(
        pull_request=pr,
        type__in=[
            PRTimelineEventType.LABELED,
            PRTimelineEventType.UNLABELED,
            PRTimelineEventType.READY_FOR_REVIEW,
            PRTimelineEventType.CONVERT_TO_DRAFT,
            PRTimelineEventType.REOPENED,
            PRTimelineEventType.CLOSED,
        ],
    ).order_by("occurred_at", "id")
    for ev in qs:
        yield ev


def _queue_windows_with_rules(pr: PullRequest, *, rules: QueueRules, as_of: datetime) -> List[QueueWindow]:
    state = _initial_state(pr)
    t0 = pr.gh_created_at
    current_on = rules.is_on_queue(is_open=state.is_open, is_draft=state.is_draft, labels=state.labels, ci_ok=state.ci_ok)
    current_start: Optional[datetime] = t0 if current_on else None
    windows: List[QueueWindow] = []

    for ev in _iter_state_events(pr):
        ts = ev.occurred_at
        if ts < t0:
            continue

        if ev.type in (PRTimelineEventType.LABELED, PRTimelineEventType.UNLABELED):
            name = _normalize_label(ev.label_name)
            if not name:
                continue
            if ev.type == PRTimelineEventType.LABELED:
                state.labels.add(name)
            else:
                state.labels.discard(name)
        elif ev.type == PRTimelineEventType.READY_FOR_REVIEW:
            state.is_draft = False
        elif ev.type == PRTimelineEventType.CONVERT_TO_DRAFT:
            state.is_draft = True
        elif ev.type == PRTimelineEventType.REOPENED:
            state.is_open = True
        elif ev.type == PRTimelineEventType.CLOSED:
            state.is_open = False

        new_on = rules.is_on_queue(is_open=state.is_open, is_draft=state.is_draft, labels=state.labels, ci_ok=state.ci_ok)
        if current_on and not new_on:
            if current_start is not None and current_start < ts:
                windows.append((current_start, ts))
            current_start = None
        elif not current_on and new_on:
            current_start = ts
        current_on = new_on

    end = as_of
    closed_ts = pr.closed_at or pr.merged_at
    if closed_ts is not None and closed_ts < end:
        end = closed_ts

    if current_on and current_start is not None and current_start < end:
        windows.append((current_start, end))

    return windows


def queue_windows_for_pr(pr: PullRequest, *, as_of: Optional[datetime] = None) -> List[QueueWindow]:
    """Return [enter, exit) windows when ``pr`` was on the queue, using QueueRuleSet."""
    if as_of is None:
        as_of = timezone.now()
    rules = load_rules_for_repo(pr.repository)
    return _queue_windows_with_rules(pr, rules=rules, as_of=as_of)


def total_queue_time_for_pr(pr: PullRequest, *, as_of: Optional[datetime] = None) -> QueueSummary:
    """Compute total time on the queue for a PR from creation until ``as_of``."""
    if as_of is None:
        as_of = timezone.now()
    windows = queue_windows_for_pr(pr, as_of=as_of)
    total_seconds = sum((end - start).total_seconds() for start, end in windows)
    return QueueSummary(pr=pr, as_of=as_of, total_seconds=total_seconds)


def is_on_queue_at(pr: PullRequest, *, at: datetime) -> bool:
    """Return True if ``pr`` was on the queue at instant ``at``."""
    rules = load_rules_for_repo(pr.repository)
    windows = _queue_windows_with_rules(pr, rules=rules, as_of=at)
    for start, end in windows:
        # For membership at a single instant, treat the upper bound as inclusive
        # when windows are computed with horizon == ``at``.
        if start <= at <= end:
            return True
    return False


def who_was_on_queue_at(*, repo: Repository, at: datetime, prs: Optional[Iterable[PullRequest]] = None) -> List[PullRequest]:
    """Return PRs in ``repo`` that were on the queue at ``at``."""
    if prs is None:
        qs = PullRequest.objects.filter(repository=repo)
    else:
        qs = prs
    return [pr for pr in qs if is_on_queue_at(pr, at=at)]


def rebuild_queue_windows_for_ruleset(
    *,
    pr: PullRequest,
    rule_set: QueueRuleSet,
    as_of: Optional[datetime] = None,
) -> QueueWindowRebuildResult:
    """Rebuild PRQueueWindow rows for a PR under a specific QueueRuleSet.

    - Computes queue windows using the exact rules from ``rule_set``.
    - Upserts windows keyed by (pull_request, rule_set, from_ts) with a monotone
      ``cycle_index`` starting at 0 for the earliest window.
    - Deletes any stale windows for this (pr, rule_set) pair whose ``from_ts`` is
      not present in the newly computed set.
    """
    if pr.repository_id != rule_set.repository_id:
        raise ValueError("QueueRuleSet.repository must match PullRequest.repository")
    if as_of is None:
        as_of = timezone.now()

    rules = rules_for_rule_set(rule_set)
    windows = _queue_windows_with_rules(pr, rules=rules, as_of=as_of)

    created = 0
    updated = 0

    expected_starts: List[datetime] = []
    for cycle_index, (start, end) in enumerate(windows):
        expected_starts.append(start)
        obj, was_created = PRQueueWindow.objects.get_or_create(
            pull_request=pr,
            rule_set=rule_set,
            from_ts=start,
            defaults={"to_ts": end, "cycle_index": cycle_index},
        )
        if was_created:
            created += 1
        else:
            changed = False
            if obj.to_ts != end:
                obj.to_ts = end
                changed = True
            if obj.cycle_index != cycle_index:
                obj.cycle_index = cycle_index
                changed = True
            if changed:
                obj.save(update_fields=["to_ts", "cycle_index"])
                updated += 1

    qs = PRQueueWindow.objects.filter(pull_request=pr, rule_set=rule_set)
    if expected_starts:
        qs = qs.exclude(from_ts__in=expected_starts)
    deleted, _ = qs.delete()

    return QueueWindowRebuildResult(created=created, updated=updated, deleted=deleted)
