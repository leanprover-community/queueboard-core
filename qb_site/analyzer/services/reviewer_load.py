"""Per-reviewer review-load, as of the latest queue snapshot.

Single authority for "how loaded is this reviewer in this repo?", shared by the reviewer-facing
surfaces (the ``assigned-prs`` Zulip command and the daily reviewer-attention digest). The load
figure mirrors the capacity accounting the assignment *engine* already computes
(``analyzer.services.reviewer_assignment`` / ``reviewer_assignment_engine``), so the number a
reviewer sees is the same one that gates whether they get auto-assigned:

- ``current_load`` is the engine's **weighted** load (status-weighted, self-authored PRs excluded),
  and also folds in the reviewer's pending assignment proposals (design doc 050), which occupy
  capacity exactly as the engine / area stats count them. ``maximum_capacity - current_load`` is the
  reviewer's remaining capacity.
- ``assigned_open`` is the raw count of open PRs they are assigned to (proposals are load, not
  assignees, so they are *not* counted here), kept alongside for human context (the gap between it
  and ``current_load`` reflects zero-weight PRs and pending proposals).
- ``weekly_count`` / ``weekly_limit`` are the *flow* gate (design doc 054): distinct PRs newly
  assigned to them in the trailing ``ANALYZER_ASSIGNMENT_RATE_WINDOW_DAYS``, against their opt-in
  ``max_new_assignments_per_week``. They come off the same ``ReviewerProfile`` the engine gate
  reads, so the reason a reviewer's push went quiet is exactly the number they are shown. This
  surfacing is load-bearing, not decoration: a rate limit that silently withholds work with no
  visible cause is indistinguishable from the pipeline being broken.

This module deliberately does **not** re-derive load math: it reads the cached queue snapshot (the
same one ``pr_info`` uses) and folds ``collect_assignment_statistics`` output against reviewer
capacities. It never *builds* a snapshot — that is the refresh task's job — so callers must treat a
missing snapshot as "no load line" rather than a zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Mapping

from django.conf import settings

from analyzer.models import QueueSnapshot
from analyzer.services.assignment_rate_limit import assignment_rate_window_days
from analyzer.services.queue_rules import default_rule_set_for_repo
from analyzer.services.reviewer_assignment import (
    _compute_weight,
    add_pending_proposal_load,
    build_reviewer_catalog,
    collect_assignment_statistics,
    pending_proposal_load_for_repo,
)
from analyzer.services.reviewer_assignment_engine import ReviewerProfile
from core.models import Repository

# Float slack for the capacity comparison: weighted loads are floats, so treat anything within an
# epsilon of the cap as "no room left" — matching the engine's ``remaining > 0`` availability gate.
_CAPACITY_EPSILON = 1e-9


def normalize_login(login: str | None) -> str:
    return (login or "").strip().lower()


@dataclass(frozen=True)
class ReviewerLoad:
    """A reviewer's load standing in one repository (see module docstring).

    The two capacity gates are orthogonal and both reported: ``current_load``/``capacity`` is the
    concurrent stock, ``weekly_count``/``weekly_limit`` the rolling-window flow. A reviewer can be
    well under one and blocked by the other.
    """

    repository_id: int
    reviewer_login: str  # normalized (lowercase)
    assigned_open: int
    current_load: float
    capacity: int
    remaining: float
    at_capacity: bool
    # Rolling-window intake (design doc 054). ``weekly_count`` is always populated — it is what a
    # reviewer needs in order to pick a limit at all — while ``weekly_limit`` is None until they
    # opt in, and ``at_weekly_limit`` is then always False.
    weekly_count: int = 0
    weekly_limit: int | None = None
    at_weekly_limit: bool = False


def compute_reviewer_loads(
    *,
    repository_id: int,
    assignments: Mapping[str, tuple[list[int], float, int]],
    reviewers: Iterable[ReviewerProfile],
) -> dict[str, ReviewerLoad]:
    """Pure fold of engine assignment stats + reviewer capacities into per-reviewer loads.

    ``assignments`` is ``AssignmentStatistics.assignments`` (``login -> (open_pr_numbers,
    weighted_load, total_assigned)``). Assignment keys are matched to reviewers case-insensitively.
    The result is keyed by normalized login and includes *every* reviewer in ``reviewers`` — a
    reviewer with nothing assigned gets a zero load (so callers can render "Load: 0 / N").

    The rolling-window figures are read straight off the profiles rather than re-queried, so the
    line a reviewer is shown is by construction the one the engine gate applied to them.
    """
    weighted_by_login: dict[str, float] = {}
    open_count_by_login: dict[str, int] = {}
    for login, (open_list, weighted, _total) in assignments.items():
        norm = normalize_login(login)
        if not norm:
            continue
        weighted_by_login[norm] = weighted_by_login.get(norm, 0.0) + float(weighted)
        open_count_by_login[norm] = open_count_by_login.get(norm, 0) + len(open_list)

    loads: dict[str, ReviewerLoad] = {}
    for reviewer in reviewers:
        norm = normalize_login(reviewer.github_login)
        if not norm or norm in loads:
            continue
        current_load = weighted_by_login.get(norm, 0.0)
        capacity = int(reviewer.maximum_capacity)
        remaining = capacity - current_load
        weekly_limit = None if reviewer.weekly_limit is None else int(reviewer.weekly_limit)
        weekly_count = int(reviewer.recent_assignment_count)
        loads[norm] = ReviewerLoad(
            repository_id=int(repository_id),
            reviewer_login=norm,
            assigned_open=open_count_by_login.get(norm, 0),
            current_load=current_load,
            capacity=capacity,
            remaining=remaining,
            at_capacity=remaining <= _CAPACITY_EPSILON,
            weekly_count=weekly_count,
            weekly_limit=weekly_limit,
            # Mirrors the engine's strict `recent + simulated < limit`: a reviewer *at* their limit
            # has spent it, so this is `>=`, not `>`.
            at_weekly_limit=weekly_limit is not None and weekly_count >= weekly_limit,
        )
    return loads


def build_reviewer_loads(
    repository: Repository,
    *,
    snapshot_payload: dict | None = None,
    now: datetime | None = None,
) -> dict[str, ReviewerLoad]:
    """Per-reviewer load for a repo, keyed by normalized login.

    Reads the latest cached queue snapshot (same resolution as ``pr_info``) and folds in pending
    assignment-proposal load (design doc 050) so the figure matches the engine's capacity gate.
    Returns ``{}`` when no snapshot or no reviewers are available — callers should render no load line
    in that case rather than fabricate a count. Read-only: never builds a snapshot.
    """
    payload = snapshot_payload if snapshot_payload is not None else _latest_snapshot_payload(repository)
    if not payload:
        return {}
    reviewers = build_reviewer_catalog(repository, now=now)
    if not reviewers:
        return {}
    stats = collect_assignment_statistics(payload)
    # Pending proposals occupy capacity exactly as the assignment engine / area stats count them
    # (design doc 050): fold their weighted load in so ``current_load`` matches the gate that governs
    # auto-assignment. Data-driven — no active proposals -> no change.
    proposal_weight = float(getattr(settings, "ANALYZER_ASSIGNMENT_PROPOSAL_PENDING_LOAD_WEIGHT", 1.0))
    assignments = add_pending_proposal_load(
        stats.assignments,
        pending_proposal_load_for_repo(repository, weight=proposal_weight),
    )
    return compute_reviewer_loads(
        repository_id=int(repository.id),
        assignments=assignments,
        reviewers=reviewers,
    )


def reviewer_load_for(
    repository: Repository,
    reviewer_login: str,
    *,
    snapshot_payload: dict | None = None,
    now: datetime | None = None,
) -> ReviewerLoad | None:
    """One reviewer's load for a repo, or ``None`` when unavailable."""
    loads = build_reviewer_loads(repository, snapshot_payload=snapshot_payload, now=now)
    return loads.get(normalize_login(reviewer_login))


def pr_load_breakdown(payload: dict, reviewer_login: str) -> dict[int, float]:
    """Per-PR weighted load contribution for one reviewer, from a queue-snapshot payload.

    Mirrors the per-PR fold inside ``collect_assignment_statistics`` (status-weighted via the shared
    ``_compute_weight``, self-authored PRs contributing 0) but scoped to a single reviewer, so the
    values sum to that reviewer's assigned-PR share of ``current_load``. Pending proposals are load
    too, but they are not snapshot assignees, so they are not included here — callers add the
    proposal weight (``ANALYZER_ASSIGNMENT_PROPOSAL_PENDING_LOAD_WEIGHT``) separately. Keyed by PR
    number.
    """
    norm = normalize_login(reviewer_login)
    if not norm:
        return {}
    breakdown: dict[int, float] = {}
    for pr_number_raw, entry in (payload.get("prs", {}) or {}).items():
        assignees = entry.get("assignees") or []
        if norm not in {normalize_login(login) for login in assignees if login}:
            continue
        try:
            pr_number = int(pr_number_raw)
        except (TypeError, ValueError):
            continue
        author_norm = normalize_login(entry.get("author") or "")
        breakdown[pr_number] = 0.0 if norm == author_norm else _compute_weight(pr_number, entry)
    return breakdown


def reviewer_load_with_breakdown(
    repository: Repository,
    reviewer_login: str,
    *,
    now: datetime | None = None,
) -> tuple[ReviewerLoad | None, dict[int, float]]:
    """One reviewer's load plus its per-PR weight breakdown, from a single snapshot read.

    Both halves derive from the same cached queue snapshot, so the per-PR contributions sum to the
    assigned-PR portion of the aggregate ``current_load`` (pending-proposal load is the remainder).
    Returns ``(None, {})`` when no snapshot is available.
    """
    payload = _latest_snapshot_payload(repository)
    if not payload:
        return None, {}
    load = reviewer_load_for(repository, reviewer_login, snapshot_payload=payload, now=now)
    return load, pr_load_breakdown(payload, reviewer_login)


def _latest_snapshot_payload(repository: Repository) -> dict | None:
    rule_set = default_rule_set_for_repo(repository)
    cache_key = str(rule_set.id) if rule_set else "default"
    snapshot = (
        QueueSnapshot.objects.filter(repository=repository, cache_key=cache_key).order_by("-generated_at").only("payload").first()
    )
    if snapshot is None or not snapshot.payload:
        return None
    return snapshot.payload


# --- formatting --------------------------------------------------------------


def _fmt_load_number(value: float) -> str:
    """Integer when whole (``3``), else one decimal (``4.5``)."""
    rounded = round(float(value), 1)
    if abs(rounded - round(rounded)) < _CAPACITY_EPSILON:
        return str(int(round(rounded)))
    return f"{rounded:.1f}"


def format_load_contribution(weight: float) -> str:
    """Render one PR's load contribution as a signed figure (``+1`` / ``+0.1`` / ``+0``).

    Uses the same number formatting as the aggregate load line, so a per-PR ``+1`` reads consistently
    with the ``Load: 3 / 5`` it rolls up into.
    """
    return f"+{_fmt_load_number(weight)}"


def format_rate_limit_segment(load: ReviewerLoad) -> str:
    """Render the rolling-window intake segment, or ``""`` for a reviewer with no limit.

    ``· last 7 days: 4 / 5``, or ``· last 7 days: 5 / 5 ⚠ weekly limit reached`` once spent. Wording
    is deliberately "last N days" rather than "this week": the window is rolling, so a reviewer
    blocked on a Monday morning would otherwise reasonably expect a fresh budget. The day count is
    read from the setting that actually defines the window, so the copy cannot drift from the
    mechanism.

    Empty for ``weekly_limit is None``, which is every reviewer until they opt in — the load line is
    byte-for-byte unchanged for them (design doc 054, Invariant 2).
    """
    if load.weekly_limit is None:
        return ""
    days = assignment_rate_window_days()
    segment = f" · last {days} days: {load.weekly_count} / {load.weekly_limit}"
    if load.at_weekly_limit:
        segment += " ⚠ weekly limit reached"
    return segment


def format_load_line(load: ReviewerLoad, *, include_assigned_count: bool = False) -> str:
    """Render the one-line load summary.

    ``Load: 3 / 10 (7 free)`` normally; ``Load: 10 / 10 ⚠ at capacity`` when full (or over). With
    ``include_assigned_count`` (the daily digest, which never lists the full roster), append
    ``· N assigned``. A reviewer who has opted into a rolling-window rate limit also gets the
    intake segment (design doc 054), e.g. ``Load: 3 / 10 (7 free) · last 7 days: 5 / 5 ⚠ weekly
    limit reached`` — the two gates are independent, so being flush with concurrent capacity while
    blocked on flow is a normal, and otherwise baffling, state to be in.

    ``at_capacity`` mirrors the engine's strict ``remaining > 0`` assignability gate, so a reviewer
    with any real room (e.g. 9.96/10) is *not* full and can still be assigned. Two display rules keep
    that honest for such near-cap loads:

    - ``free`` is derived from the shown ``used`` (``capacity - used``), so the two always sum to the
      capacity instead of drifting apart under independent rounding.
    - a ``used`` that rounds up to the capacity while the reviewer is not actually full is clamped to
      ``capacity - 0.1``, so real (if tiny) room never renders as the contradictory ``10 / 10
      (0 free)`` — it shows ``9.9 / 10 (0.1 free)`` instead.
    """
    cap = str(load.capacity)
    if load.at_capacity:
        # Show the true (possibly over-capacity) load next to the marker.
        line = f"Load: {_fmt_load_number(load.current_load)} / {cap} ⚠ at capacity"
    else:
        used_val = round(load.current_load, 1)
        if used_val >= load.capacity:
            used_val = load.capacity - 0.1
        free_val = round(load.capacity - used_val, 1)
        line = f"Load: {_fmt_load_number(used_val)} / {cap} ({_fmt_load_number(free_val)} free)"
    if include_assigned_count:
        line += f" · {load.assigned_open} assigned"
    line += format_rate_limit_segment(load)
    return line
