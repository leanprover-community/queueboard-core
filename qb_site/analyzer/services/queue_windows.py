from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, List, Optional, Tuple

from django.db import models
from django.utils import timezone

from core.models import Repository
from analyzer.models import PRQueueWindow, QueueRuleSet, PRRevision
from analyzer.services.queue_rules import QueueRules, load_rules_for_repo, rules_for_rule_set
from syncer.models import CheckRun, PullRequest, PRTimelineEvent, PRTimelineEventType, StatusContext


QueueWindow = Tuple[datetime, Optional[datetime]]


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
    status: str = "rebuilt"
    reason: str | None = None


def _normalize_label(name: str | None) -> str:
    return (name or "").strip().lower()


def _merge_latest_ci_status(
    latest: dict[str, tuple[datetime, bool]],
    *,
    name: str | None,
    ts: datetime | None,
    ok: bool,
) -> None:
    if not name or ts is None:
        return
    key = name.strip().lower()
    if not key:
        return
    current = latest.get(key)
    if current is None or ts > current[0]:
        latest[key] = (ts, ok)


def _latest_ci_statuses_for_fragment(
    pr: PullRequest,
    *,
    required_fragment: str,
    at: datetime,
    head_sha: str | None,
) -> dict[str, bool]:
    required_fragment = (required_fragment or "").strip().lower()
    if not required_fragment:
        return {}
    latest: dict[str, tuple[datetime, bool]] = {}

    cr_qs = CheckRun.objects.filter(pull_request=pr, name__icontains=required_fragment, gh_completed_at__lte=at)
    if head_sha:
        cr_qs = cr_qs.filter(head_sha=head_sha)
    for cr in cr_qs:
        ts = cr.gh_completed_at or cr.gh_started_at
        _merge_latest_ci_status(latest, name=cr.name, ts=ts, ok=_check_run_ok(cr))

    sc_qs = StatusContext.objects.filter(pull_request=pr, name__icontains=required_fragment, gh_created_at__lte=at)
    if head_sha:
        sc_qs = sc_qs.filter(head_sha=head_sha)
    for sc in sc_qs:
        _merge_latest_ci_status(latest, name=sc.name, ts=sc.gh_created_at, ok=_status_context_ok(sc))

    return {name: status for name, (_, status) in latest.items()}


@dataclass
class _State:
    labels: set[str]
    is_draft: bool
    is_open: bool
    ci_ok: Optional[bool]


def _initial_state(pr: PullRequest, *, created_as_draft: bool) -> _State:
    # Start with empty label set; label history is derived solely from timeline events.
    # Treat the PR as open from creation until closed/merged (or until explicit
    # CLOSED/REOPENED timeline events say otherwise). This avoids relying on the
    # current ``state`` field when reconstructing historical windows.
    return _State(labels=set(), is_draft=created_as_draft, is_open=True, ci_ok=None)


def _created_as_draft(pr: PullRequest, timeline_events: Iterable[PRTimelineEvent]) -> bool:
    """Infer draft state at creation.

    GitHub does not emit ConvertToDraft events at creation time, so a PR created as
    draft only shows up later via a ReadyForReview event. The first draft-related
    event therefore determines the initial draft state.
    """
    for ev in timeline_events:
        if ev.type == PRTimelineEventType.READY_FOR_REVIEW:
            return True
        if ev.type == PRTimelineEventType.CONVERT_TO_DRAFT:
            return False
    return bool(pr.is_draft)


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


def _state_at_time(pr: PullRequest, *, at: datetime) -> _State:
    """Compute label/open/draft state for a PR at a specific instant."""
    timeline_events = list(_iter_state_events(pr))
    state = _initial_state(pr, created_as_draft=_created_as_draft(pr, timeline_events))
    t0 = pr.gh_created_at
    for ev in timeline_events:
        ts = ev.occurred_at
        if ts < t0:
            continue
        if ts > at:
            break

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

    closed_ts = pr.closed_at or pr.merged_at
    if closed_ts is not None and closed_ts <= at:
        state.is_open = False

    return state


def _check_run_ok(cr: CheckRun) -> bool:
    """Return True if a CheckRun snapshot counts as successful for queue gating."""
    # Only COMPLETED runs are considered final; others keep the PR off the queue.
    if cr.status != "COMPLETED":
        return False
    # Treat SUCCESS / NEUTRAL / SKIPPED as acceptable; failures and infrastructure issues block.
    if cr.conclusion in ("SUCCESS", "NEUTRAL", "SKIPPED"):
        return True
    return False


def _status_context_ok(sc: StatusContext) -> bool:
    """Return True if a StatusContext snapshot counts as successful for queue gating."""
    # SUCCESS is ok; FAILURE/ERROR/PENDING keep the PR off the queue.
    return sc.state == "SUCCESS"


def _head_sha_at_time(pr: PullRequest, *, at: datetime) -> tuple[Optional[str], bool]:
    """Return the head SHA for a PR at time ``at`` using PRRevision.

    Returns a pair (head_sha, has_revisions):
    - head_sha: the SHA for the revision window containing ``at``, or None.
    - has_revisions: True iff any PRRevision rows exist for this PR.
    """
    qs = PRRevision.objects.filter(pull_request=pr)
    has_revisions = qs.exists()
    if not has_revisions:
        return None, False
    rev = qs.filter(from_ts__lte=at).order_by("-from_ts", "-seq", "-id").first()
    if rev is None:
        return None, True
    if rev.to_ts is not None and at >= rev.to_ts:
        # Outside this window; conservative fallback.
        return None, True
    return (rev.head_sha or None, True)


def _ci_required_contexts_ok(pr: PullRequest, rules: QueueRules, at: datetime) -> bool:
    """Return True iff CI satisfies rules.required_ci_contexts for this PR.

    Semantics
    - If CI is not required (`require_ci_success` is False), CI always counts as ok.
    - If `required_ci_contexts` is empty, CI gating is treated as disabled for now.
    - If PRRevision rows exist for this PR, we use the head SHA at time ``at`` and
      look only at CI snapshots for that SHA.
    - If no PRRevision rows exist, we fall back to per-PR snapshots (no head filter),
      matching the initial non-revision behavior.
    - For each required context name, we look for the latest snapshot on this PR
      at or before ``at`` (CheckRun or StatusContext, case-insensitive substring
      match on name, and matching head SHA when available) and require it to be in
      a successful state.
    - Missing or non-successful snapshots for any required context cause CI to be
      treated as not ok.

    Notes
    - When PRRevision exists, this function is head- and time-aware: CI snapshots
      are evaluated against the head SHA that was current at ``at``.
    - We still approximate intra-head flapping by using the latest snapshot per
      context at or before ``at``.
    """
    if not rules.require_ci_success:
        return True

    required = rules.required_ci_contexts or set()
    if not required:
        # No explicit contexts configured; treat CI gating as disabled.
        return True

    head_sha, has_revisions = _head_sha_at_time(pr, at=at)
    # If we have revisions but cannot resolve a head at this time, treat CI as unknown.
    if has_revisions and not head_sha:
        return False

    ok = True
    for ctx_name in required:
        ctx_norm = _normalize_label(ctx_name)
        latest_statuses = _latest_ci_statuses_for_fragment(pr, required_fragment=ctx_norm, at=at, head_sha=head_sha)
        if not latest_statuses:
            ok = False
            break
        if not all(latest_statuses.values()):
            return False

    return ok


def _queue_windows_with_rules(pr: PullRequest, *, rules: QueueRules, as_of: datetime) -> List[QueueWindow]:
    timeline_events = list(_iter_state_events(pr))
    state = _initial_state(pr, created_as_draft=_created_as_draft(pr, timeline_events))
    t0 = pr.gh_created_at

    # Label-only rulesets (no CI gating) can use a simpler builder that ignores CI.
    if not rules.require_ci_success:
        current_on = rules.is_on_queue(is_open=state.is_open, is_draft=state.is_draft, labels=state.labels, ci_ok=True)
        current_start: Optional[datetime] = t0 if current_on else None
        windows: List[QueueWindow] = []

        for ev in timeline_events:
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

            new_on = rules.is_on_queue(is_open=state.is_open, is_draft=state.is_draft, labels=state.labels, ci_ok=True)
            if current_on and not new_on:
                if current_start is not None and current_start < ts:
                    windows.append((current_start, ts))
                current_start = None
            elif not current_on and new_on:
                current_start = ts
            current_on = new_on

        end = None
        closed_ts = pr.closed_at or pr.merged_at
        if closed_ts is not None and closed_ts <= as_of:
            end = closed_ts

        if current_on and current_start is not None:
            if end is None:
                if current_start < as_of:
                    windows.append((current_start, None))
            elif current_start < end:
                windows.append((current_start, end))

        return windows

    # CI-gated rulesets: preload revisions/CI snapshots once, then evaluate queue
    # membership incrementally at each boundary.
    closed_ts = pr.closed_at or pr.merged_at
    required_ci = sorted({_normalize_label(name) for name in (rules.required_ci_contexts or set()) if _normalize_label(name)})

    revisions = list(PRRevision.objects.filter(pull_request=pr).order_by("from_ts", "seq", "id"))
    has_revisions = bool(revisions)

    check_run_qs = CheckRun.objects.filter(
        pull_request=pr,
        gh_completed_at__isnull=False,
        gh_completed_at__gte=t0,
        gh_completed_at__lte=as_of,
    ).only("id", "name", "head_sha", "status", "conclusion", "gh_completed_at", "gh_started_at")
    status_qs = StatusContext.objects.filter(
        pull_request=pr,
        gh_created_at__gte=t0,
        gh_created_at__lte=as_of,
    ).only("id", "name", "head_sha", "state", "gh_created_at")

    if required_ci:
        ctx_q = models.Q()
        for ctx in required_ci:
            ctx_q |= models.Q(name__icontains=ctx)
        check_run_qs = check_run_qs.filter(ctx_q)
        status_qs = status_qs.filter(ctx_q)
    else:
        check_run_qs = check_run_qs.none()
        status_qs = status_qs.none()

    check_runs = list(check_run_qs.order_by("gh_completed_at", "id"))
    statuses = list(status_qs.order_by("gh_created_at", "id"))

    # Collect boundary times where queue membership may change.
    boundary_times: set[datetime] = {t0, as_of}
    for ev in timeline_events:
        if ev.occurred_at >= t0 and ev.occurred_at <= as_of:
            boundary_times.add(ev.occurred_at)
    for rev in revisions:
        if rev.from_ts >= t0 and rev.from_ts <= as_of:
            boundary_times.add(rev.from_ts)
    for cr in check_runs:
        if cr.gh_completed_at is not None:
            boundary_times.add(cr.gh_completed_at)
    for sc in statuses:
        boundary_times.add(sc.gh_created_at)
    if closed_ts is not None and closed_ts >= t0 and closed_ts <= as_of:
        boundary_times.add(closed_ts)

    times = sorted(boundary_times)

    # Latest CI status maps:
    # - global: when revisions are absent (legacy fallback).
    # - per-head: when revisions are present (head-aware evaluation).
    global_ctx_latest: dict[str, dict[str, tuple[datetime, bool, str]]] = {ctx: {} for ctx in required_ci}
    head_ctx_latest: dict[str, dict[str, dict[str, tuple[datetime, bool, str]]]] = {}

    def _merge_latest_for_ctx(
        latest: dict[str, tuple[datetime, bool, str]],
        *,
        name: str,
        ts: datetime,
        ok: bool,
        source: str,
    ) -> None:
        # Match existing behavior from _latest_ci_statuses_for_fragment:
        # latest timestamp wins; if tied, prefer CheckRun over StatusContext.
        cur = latest.get(name)
        if cur is None:
            latest[name] = (ts, ok, source)
            return
        cur_ts, _, cur_source = cur
        if ts > cur_ts or (ts == cur_ts and source == "check_run" and cur_source == "status_context"):
            latest[name] = (ts, ok, source)

    def _apply_ci_snapshot(*, name: str | None, ts: datetime | None, head_sha: str | None, ok: bool, source: str) -> None:
        if ts is None:
            return
        name_norm = _normalize_label(name)
        if not name_norm:
            return
        matched = [ctx for ctx in required_ci if ctx in name_norm]
        if not matched:
            return
        for ctx in matched:
            _merge_latest_for_ctx(global_ctx_latest[ctx], name=name_norm, ts=ts, ok=ok, source=source)
        if head_sha:
            per_head = head_ctx_latest.setdefault(head_sha, {ctx: {} for ctx in required_ci})
            for ctx in matched:
                _merge_latest_for_ctx(per_head[ctx], name=name_norm, ts=ts, ok=ok, source=source)

    windows: List[QueueWindow] = []
    event_idx = 0
    rev_idx = 0
    current_rev: PRRevision | None = None
    cr_idx = 0
    sc_idx = 0
    current_on: Optional[bool] = None
    current_start: Optional[datetime] = None

    for t in times:
        while event_idx < len(timeline_events) and timeline_events[event_idx].occurred_at <= t:
            ev = timeline_events[event_idx]
            event_idx += 1
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

        while rev_idx < len(revisions) and revisions[rev_idx].from_ts <= t:
            current_rev = revisions[rev_idx]
            rev_idx += 1

        while (
            cr_idx < len(check_runs)
            and check_runs[cr_idx].gh_completed_at is not None
            and check_runs[cr_idx].gh_completed_at <= t
        ):
            cr = check_runs[cr_idx]
            cr_idx += 1
            _apply_ci_snapshot(
                name=cr.name,
                ts=cr.gh_completed_at or cr.gh_started_at,
                head_sha=cr.head_sha,
                ok=_check_run_ok(cr),
                source="check_run",
            )

        while sc_idx < len(statuses) and statuses[sc_idx].gh_created_at <= t:
            sc = statuses[sc_idx]
            sc_idx += 1
            _apply_ci_snapshot(
                name=sc.name,
                ts=sc.gh_created_at,
                head_sha=sc.head_sha,
                ok=_status_context_ok(sc),
                source="status_context",
            )

        if closed_ts is not None and t >= closed_ts:
            state.is_open = False

        if not required_ci:
            ci_ok = True
        else:
            head_sha: str | None = None
            if has_revisions:
                if current_rev is not None and (current_rev.to_ts is None or t < current_rev.to_ts):
                    head_sha = current_rev.head_sha or None
                if not head_sha:
                    ci_ok = False
                    new_on = rules.is_on_queue(is_open=state.is_open, is_draft=state.is_draft, labels=state.labels, ci_ok=ci_ok)
                    if current_on is None:
                        current_on = new_on
                        if new_on:
                            current_start = t
                        continue
                    if current_on and not new_on:
                        if current_start is not None and current_start < t:
                            windows.append((current_start, t))
                        current_start = None
                    elif not current_on and new_on:
                        current_start = t
                    current_on = new_on
                    continue

            ci_ok = True
            for ctx in required_ci:
                if has_revisions:
                    statuses_for_ctx = (head_ctx_latest.get(head_sha or "", {}) or {}).get(ctx, {})
                else:
                    statuses_for_ctx = global_ctx_latest.get(ctx, {})
                if not statuses_for_ctx:
                    ci_ok = False
                    break
                if not all(ok for _, ok, _ in statuses_for_ctx.values()):
                    ci_ok = False
                    break

        new_on = rules.is_on_queue(is_open=state.is_open, is_draft=state.is_draft, labels=state.labels, ci_ok=ci_ok)

        if current_on is None:
            current_on = new_on
            if new_on:
                current_start = t
            continue

        if current_on and not new_on:
            if current_start is not None and current_start < t:
                windows.append((current_start, t))
            current_start = None
        elif not current_on and new_on:
            current_start = t

        current_on = new_on

    # Close any trailing open window; use None for open-ended windows.
    if current_on and current_start is not None:
        end = None
        if closed_ts is not None and closed_ts <= as_of:
            end = closed_ts
        if end is None:
            if current_start < as_of:
                windows.append((current_start, None))
        elif current_start < end:
            windows.append((current_start, end))

    return windows


def queue_windows_for_pr(pr: PullRequest, *, as_of: Optional[datetime] = None) -> List[QueueWindow]:
    """Return [enter, exit) windows when ``pr`` was on the queue, using QueueRuleSet."""
    if as_of is None:
        as_of = timezone.now()
    rules = load_rules_for_repo(pr.repository, at=as_of)
    return _queue_windows_with_rules(pr, rules=rules, as_of=as_of)


def total_queue_time_for_pr(pr: PullRequest, *, as_of: Optional[datetime] = None) -> QueueSummary:
    """Compute total time on the queue for a PR from creation until ``as_of``."""
    if as_of is None:
        as_of = timezone.now()
    windows = queue_windows_for_pr(pr, as_of=as_of)
    total_seconds = sum(((end or as_of) - start).total_seconds() for start, end in windows)
    return QueueSummary(pr=pr, as_of=as_of, total_seconds=total_seconds)


def is_on_queue_at(pr: PullRequest, *, at: datetime) -> bool:
    """Return True if ``pr`` was on the queue at instant ``at``."""
    rules = load_rules_for_repo(pr.repository, at=at)
    state = _state_at_time(pr, at=at)
    ci_ok = _ci_required_contexts_ok(pr, rules, at)
    return rules.is_on_queue(is_open=state.is_open, is_draft=state.is_draft, labels=state.labels, ci_ok=ci_ok)


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

    # Do not build queue windows unless we know the underlying data is complete
    # enough for the horizon we are analyzing. This avoids persisting windows
    # that would need to be invalidated when backfills complete.
    #
    # Preconditions:
    # - Timeline backfill must be complete so that label/draft/open/closed
    #   history is stable.
    # - For rulesets that require CI success, PRRevision must exist so that
    #   head SHA windows are known. CI completeness is assumed to be enforced
    #   by the operator via Analyzer backfill commands.
    if not getattr(pr, "timeline_backfill_done", False):
        # Ensure we do not leave stale windows around if gating conditions
        # change over time.
        PRQueueWindow.objects.filter(pull_request=pr, rule_set=rule_set).delete()
        return QueueWindowRebuildResult(
            created=0,
            updated=0,
            deleted=0,
            status="skipped",
            reason="timeline_backfill_incomplete",
        )

    if rule_set.require_ci_success:
        has_revisions = PRRevision.objects.filter(pull_request=pr).exists()
        if not has_revisions:
            PRQueueWindow.objects.filter(pull_request=pr, rule_set=rule_set).delete()
            return QueueWindowRebuildResult(
                created=0,
                updated=0,
                deleted=0,
                status="skipped",
                reason="missing_pr_revisions_for_ci_ruleset",
            )

    if as_of is None:
        as_of = timezone.now()

    rules = rules_for_rule_set(rule_set)
    windows = _queue_windows_with_rules(pr, rules=rules, as_of=as_of)

    window_count = len(windows)
    first_on_queue_ts = windows[0][0] if windows else None
    cumulative_seconds = 0

    existing_by_start = {obj.from_ts: obj for obj in PRQueueWindow.objects.filter(pull_request=pr, rule_set=rule_set)}
    to_create: list[PRQueueWindow] = []
    to_update: list[PRQueueWindow] = []

    for cycle_index, (start, end) in enumerate(windows):
        duration_seconds = 0
        if end is not None:
            duration_seconds = int((end - start).total_seconds())
        cumulative_seconds += duration_seconds
        obj = existing_by_start.pop(start, None)
        if obj is None:
            to_create.append(
                PRQueueWindow(
                    pull_request=pr,
                    rule_set=rule_set,
                    from_ts=start,
                    to_ts=end,
                    cycle_index=cycle_index,
                    duration_seconds_closed=duration_seconds,
                    cumulative_seconds_closed=cumulative_seconds,
                    window_count=window_count,
                    first_on_queue_ts=first_on_queue_ts,
                )
            )
            continue

        changed = False
        if obj.to_ts != end:
            obj.to_ts = end
            changed = True
        if obj.cycle_index != cycle_index:
            obj.cycle_index = cycle_index
            changed = True
        if obj.duration_seconds_closed != duration_seconds:
            obj.duration_seconds_closed = duration_seconds
            changed = True
        if obj.cumulative_seconds_closed != cumulative_seconds:
            obj.cumulative_seconds_closed = cumulative_seconds
            changed = True
        if obj.window_count != window_count:
            obj.window_count = window_count
            changed = True
        if obj.first_on_queue_ts != first_on_queue_ts:
            obj.first_on_queue_ts = first_on_queue_ts
            changed = True
        if changed:
            to_update.append(obj)

    if to_create:
        PRQueueWindow.objects.bulk_create(to_create, batch_size=200)
    if to_update:
        PRQueueWindow.objects.bulk_update(
            to_update,
            [
                "to_ts",
                "cycle_index",
                "duration_seconds_closed",
                "cumulative_seconds_closed",
                "window_count",
                "first_on_queue_ts",
            ],
            batch_size=200,
        )

    deleted = 0
    if existing_by_start:
        deleted, _ = PRQueueWindow.objects.filter(id__in=[obj.id for obj in existing_by_start.values()]).delete()

    return QueueWindowRebuildResult(
        created=len(to_create),
        updated=len(to_update),
        deleted=deleted,
        status="rebuilt",
        reason=None,
    )


def rebuild_queue_windows_for_pr(
    *,
    pr: PullRequest,
    rule_sets: Optional[List[QueueRuleSet]] = None,
    as_of: Optional[datetime] = None,
) -> dict[str, object]:
    rulesets = rule_sets or list(QueueRuleSet.objects.filter(repository=pr.repository, is_active=True))
    summary: dict[str, object] = {
        "rulesets": len(rulesets),
        "created": 0,
        "updated": 0,
        "deleted": 0,
        "skipped": 0,
        "skipped_out_of_bounds": 0,
        "per_ruleset": {},
    }
    created_at = pr.gh_created_at

    per_ruleset: dict[int, dict[str, object]] = {}
    for rule_set in rulesets:
        if created_at and rule_set.effective_from and created_at < rule_set.effective_from:
            per_ruleset[int(rule_set.id)] = {
                "created": 0,
                "updated": 0,
                "deleted": 0,
                "status": "skipped",
                "reason": "pr_before_ruleset_effective_from",
            }
            summary["skipped"] = int(summary["skipped"]) + 1
            summary["skipped_out_of_bounds"] = int(summary["skipped_out_of_bounds"]) + 1
            continue
        if created_at and rule_set.effective_to and created_at >= rule_set.effective_to:
            per_ruleset[int(rule_set.id)] = {
                "created": 0,
                "updated": 0,
                "deleted": 0,
                "status": "skipped",
                "reason": "pr_on_or_after_ruleset_effective_to",
            }
            summary["skipped"] = int(summary["skipped"]) + 1
            summary["skipped_out_of_bounds"] = int(summary["skipped_out_of_bounds"]) + 1
            continue

        res = rebuild_queue_windows_for_ruleset(pr=pr, rule_set=rule_set, as_of=as_of)
        per_ruleset[int(rule_set.id)] = {
            "created": int(res.created),
            "updated": int(res.updated),
            "deleted": int(res.deleted),
            "status": getattr(res, "status", None),
            "reason": getattr(res, "reason", None),
        }
        summary["created"] = int(summary["created"]) + int(res.created)
        summary["updated"] = int(summary["updated"]) + int(res.updated)
        summary["deleted"] = int(summary["deleted"]) + int(res.deleted)
        if getattr(res, "status", None) == "skipped":
            summary["skipped"] = int(summary["skipped"]) + 1

    summary["per_ruleset"] = per_ruleset
    return summary


def queue_windows_need_rollup_backfill(*, pr: PullRequest, rule_set: QueueRuleSet) -> bool:
    qs = PRQueueWindow.objects.filter(pull_request=pr, rule_set=rule_set)
    if not qs.exists():
        return False
    return qs.filter(models.Q(window_count=0) | models.Q(first_on_queue_ts__isnull=True)).exists()
