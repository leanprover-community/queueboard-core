from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from django.db.models import Q

from analyzer.models import AssignmentProposal, PRQueueWindow, QueueRuleSet
from core.models import Repository, ReviewerPreference
from core.services.reviewer_notification_settings import parse_notification_policy
from syncer.models import PRTimelineEvent, PRTimelineEventType, PullRequest


def _normalize_login(login: str | None) -> str:
    return (login or "").strip().lower()


@dataclass(frozen=True)
class ReviewerAttentionItem:
    pr_number: int
    pr_title: str
    is_on_queue: bool
    last_assigned_at: datetime | None
    queue_anchor_at: datetime | None
    days_on_queue_since_assignment: int | None
    total_queue_seconds: int | None
    total_queue_days: int | None
    needs_new_assignment_ping: bool = False
    needs_nudge: bool = False
    needs_auto_unassign: bool = False
    missing_assignment_timestamp: bool = False


@dataclass(frozen=True)
class ReviewerAttentionReport:
    reviewer_login: str
    reviewer_user_id: int
    repository_id: int
    notifications_enabled: bool
    stale_nudge_days: int
    auto_unassign_days: int
    items: tuple[ReviewerAttentionItem, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_events_of_interest(self) -> bool:
        return any(item.needs_new_assignment_ping or item.needs_nudge or item.needs_auto_unassign for item in self.items)

    @property
    def has_notifications_to_send(self) -> bool:
        return self.notifications_enabled and self.has_events_of_interest


def build_reviewer_attention_reports(
    *,
    repository: Repository,
    as_of: datetime | None = None,
    rule_set: QueueRuleSet | None = None,
    new_assignment_ping_window_seconds: int = 24 * 60 * 60,
    policy_start_at: datetime | None = None,
) -> list[ReviewerAttentionReport]:
    """Build read-only reviewer attention reports from current DB state.

    Output intentionally includes both:
    - complete status rows (`items`) suitable for on-demand reporting;
    - event flags (`needs_new_assignment_ping`, `needs_nudge`, `needs_auto_unassign`) for scheduled notifications/enforcement.
    """

    now_ts = as_of or datetime.now(timezone.utc)
    if now_ts.tzinfo is None:
        now_ts = now_ts.replace(tzinfo=timezone.utc)
    policy_start_ts = policy_start_at
    if policy_start_ts is not None and policy_start_ts.tzinfo is None:
        policy_start_ts = policy_start_ts.replace(tzinfo=timezone.utc)
    new_assignment_ping_cutoff = now_ts - timedelta(seconds=max(1, int(new_assignment_ping_window_seconds)))
    if policy_start_ts is not None and policy_start_ts > new_assignment_ping_cutoff:
        new_assignment_ping_cutoff = policy_start_ts

    active_rule_set = rule_set
    if active_rule_set is None:
        active_rule_set = QueueRuleSet.objects.filter(repository=repository, is_active=True).order_by("-version", "-id").first()

    prefs = list(
        ReviewerPreference.objects.filter(repository=repository).select_related("user").order_by("user__github_login", "id")
    )
    if not prefs:
        return []

    open_prs = list(
        PullRequest.objects.filter(repository=repository, state="open")
        .only("id", "number", "title", "assignees")
        .order_by("number")
    )

    pr_by_number: dict[int, PullRequest] = {int(pr.number): pr for pr in open_prs}
    assigned_pr_numbers_by_reviewer: dict[str, list[int]] = {}
    for pr in open_prs:
        for assignee in pr.assignees or []:
            login = _normalize_login(str(assignee))
            if not login:
                continue
            assigned_pr_numbers_by_reviewer.setdefault(login, []).append(int(pr.number))

    active_window_by_pr_number: dict[int, PRQueueWindow] = {}
    windows_by_pr_number: dict[int, list[PRQueueWindow]] = {}
    if active_rule_set is not None and pr_by_number:
        all_windows = list(
            PRQueueWindow.objects.filter(
                pull_request__in=list(pr_by_number.values()),
                rule_set=active_rule_set,
                from_ts__lte=now_ts,
            )
            .select_related("pull_request")
            .order_by("pull_request__number", "from_ts", "id")
        )
        for window in all_windows:
            pr_number = int(window.pull_request.number)
            windows_by_pr_number.setdefault(pr_number, []).append(window)

        active_windows = (
            PRQueueWindow.objects.filter(
                pull_request__in=list(pr_by_number.values()),
                rule_set=active_rule_set,
                from_ts__lte=now_ts,
            )
            .filter(Q(to_ts__isnull=True) | Q(to_ts__gt=now_ts))
            .select_related("pull_request")
            .order_by("pull_request__number", "-from_ts", "-id")
        )
        for window in active_windows:
            pr_number = int(window.pull_request.number)
            # Keep the latest active window when duplicates exist.
            active_window_by_pr_number.setdefault(pr_number, window)

    last_assignment_by_pr_and_reviewer: dict[tuple[int, str], datetime] = {}
    if pr_by_number:
        rows = (
            PRTimelineEvent.objects.filter(
                pull_request__repository=repository,
                pull_request__number__in=list(pr_by_number.keys()),
                type=PRTimelineEventType.ASSIGNED,
                assignee_login__isnull=False,
            )
            .values_list("pull_request__number", "assignee_login", "occurred_at")
            .order_by("pull_request__number", "occurred_at")
        )
        for pr_number_raw, assignee_login, occurred_at in rows:
            key = (int(pr_number_raw), _normalize_login(str(assignee_login)))
            prev = last_assignment_by_pr_and_reviewer.get(key)
            if prev is None or occurred_at > prev:
                last_assignment_by_pr_and_reviewer[key] = occurred_at

    # De-dupe against the acceptance-gate proposal DM (design doc 050): when an assignee arrived
    # because the reviewer just accepted a proposal on the console, they already got the proposal DM,
    # so suppress the redundant "newly assigned" ping for that (pr, reviewer). Keyed to the same ping
    # window as the ping itself. Data-driven: no accepted-via-console rows -> no effect.
    recently_accepted_via_console: set[tuple[int, str]] = set()
    if pr_by_number:
        accepted_rows = AssignmentProposal.objects.filter(
            repository=repository,
            pr_number__in=list(pr_by_number.keys()),
            state=AssignmentProposal.STATE_ACCEPTED,
            decided_via=AssignmentProposal.DECIDED_VIA_CONSOLE,
            decided_at__gte=new_assignment_ping_cutoff,
        ).values_list("pr_number", "reviewer_login")
        recently_accepted_via_console = {(int(n), _normalize_login(str(login))) for n, login in accepted_rows}

    reports: list[ReviewerAttentionReport] = []
    for pref in prefs:
        reviewer_login = _normalize_login(getattr(pref.user, "github_login", None))
        if not reviewer_login:
            continue

        policy = parse_notification_policy(pref.notification_settings)
        assigned_pr_numbers = sorted(set(assigned_pr_numbers_by_reviewer.get(reviewer_login, [])))
        items: list[ReviewerAttentionItem] = []
        warnings: list[str] = []

        for pr_number in assigned_pr_numbers:
            pr = pr_by_number.get(pr_number)
            if pr is None:
                continue

            active_window = active_window_by_pr_number.get(pr_number)
            is_on_queue = active_window is not None
            assignment_key = (pr_number, reviewer_login)
            last_assigned_at = last_assignment_by_pr_and_reviewer.get(assignment_key)

            queue_anchor_at: datetime | None = None
            days_since_anchor: int | None = None
            total_queue_seconds: int | None = None
            total_queue_days: int | None = None
            missing_assignment_timestamp = False
            needs_new_assignment_ping = False
            needs_nudge = False
            needs_auto_unassign = False

            windows_for_pr = windows_by_pr_number.get(pr_number)
            if windows_for_pr:
                total_seconds = _queue_total_seconds(windows_for_pr, as_of=now_ts)
                total_queue_seconds = int(total_seconds)
                total_queue_days = int(total_seconds // 86400)

            if is_on_queue:
                if last_assigned_at is None:
                    missing_assignment_timestamp = True
                    warnings.append(f"Missing assignment timestamp for PR #{pr_number}.")
                else:
                    queue_anchor_at = max(last_assigned_at, active_window.from_ts)
                    if policy_start_ts is not None and policy_start_ts > queue_anchor_at:
                        queue_anchor_at = policy_start_ts
                    total_seconds = max((now_ts - queue_anchor_at).total_seconds(), 0.0)
                    days_since_anchor = int(total_seconds // 86400)
                    needs_auto_unassign = days_since_anchor >= policy.auto_unassign_days
                    needs_nudge = (days_since_anchor >= policy.stale_nudge_days) and not needs_auto_unassign
            if (
                last_assigned_at is not None
                and last_assigned_at >= new_assignment_ping_cutoff
                and (pr_number, reviewer_login) not in recently_accepted_via_console
            ):
                needs_new_assignment_ping = True

            items.append(
                ReviewerAttentionItem(
                    pr_number=pr_number,
                    pr_title=pr.title,
                    is_on_queue=is_on_queue,
                    last_assigned_at=last_assigned_at,
                    queue_anchor_at=queue_anchor_at,
                    days_on_queue_since_assignment=days_since_anchor,
                    total_queue_seconds=total_queue_seconds,
                    total_queue_days=total_queue_days,
                    needs_new_assignment_ping=needs_new_assignment_ping,
                    needs_nudge=needs_nudge,
                    needs_auto_unassign=needs_auto_unassign,
                    missing_assignment_timestamp=missing_assignment_timestamp,
                )
            )

        reports.append(
            ReviewerAttentionReport(
                reviewer_login=reviewer_login,
                reviewer_user_id=int(pref.user_id),
                repository_id=int(repository.id),
                notifications_enabled=bool(pref.notifications_enabled),
                stale_nudge_days=policy.stale_nudge_days,
                auto_unassign_days=policy.auto_unassign_days,
                items=tuple(items),
                warnings=tuple(warnings),
            )
        )

    return reports


def _queue_total_seconds(windows: list[PRQueueWindow], *, as_of: datetime) -> int:
    total_seconds = 0.0
    for window in windows:
        start = window.from_ts
        end = window.to_ts or as_of
        if end > as_of:
            end = as_of
        if end <= start:
            continue
        total_seconds += (end - start).total_seconds()
    return int(max(total_seconds, 0.0))
