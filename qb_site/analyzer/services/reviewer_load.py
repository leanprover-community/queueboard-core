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
from analyzer.services.queue_rules import default_rule_set_for_repo
from analyzer.services.reviewer_assignment import (
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
    """A reviewer's load standing in one repository (see module docstring)."""

    repository_id: int
    reviewer_login: str  # normalized (lowercase)
    assigned_open: int
    current_load: float
    capacity: int
    remaining: float
    at_capacity: bool


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
        loads[norm] = ReviewerLoad(
            repository_id=int(repository_id),
            reviewer_login=norm,
            assigned_open=open_count_by_login.get(norm, 0),
            current_load=current_load,
            capacity=capacity,
            remaining=remaining,
            at_capacity=remaining <= _CAPACITY_EPSILON,
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


def format_load_line(load: ReviewerLoad, *, include_assigned_count: bool = False) -> str:
    """Render the one-line load summary.

    ``Load: 3 / 10 (7 free)`` normally; ``Load: 10 / 10 ⚠ at capacity`` when full (or over). With
    ``include_assigned_count`` (the daily digest, which never lists the full roster), append
    ``· N assigned``.

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
    return line
