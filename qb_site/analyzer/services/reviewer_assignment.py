from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, Sequence, Set

from django.conf import settings
from django.utils.dateparse import parse_datetime

from analyzer.models import (
    AreaStatsSnapshot,
    AssignmentProposal,
    QueueRuleSet,
    QueueSnapshot,
    ReviewerAssignmentSnapshot,
    ReviewerOptOut,
)
from analyzer.services.assignment_rate_limit import assignment_rate_window_days, recent_assignment_counts
from analyzer.services.queue_rules import default_rule_set_for_repo, rules_for_rule_set
from analyzer.services.queueboard_snapshot import QueueboardSnapshotBuilder
from analyzer.services.reviewer_assignment_engine import (
    PRAssignmentPriority,
    PRAssignmentPriorityScorer,
    ReviewerProfile,
    ReviewerSuggestionResult,
    SimulationInputs,
    _normalize_login,
    add_pending_proposal_load,
    rank_prs_for_assignment,
    run_assignment_simulation,
    suggest_reviewer_for_pr,
)
from core.models import Repository, ReviewerPreference
from core.services.topic_labels import TopicLabelMatcher, default_topic_label_matcher, topic_label_matcher_for_repo
from queueboard.classify_pr_state import PRStatus

DataStatus = str  # "valid" | "incomplete" | "missing"


def _isoformat(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _opt_outs_for_prs(repository: Repository, pr_numbers: Sequence[int]) -> dict[int, set[str]]:
    if not pr_numbers:
        return {}
    opt_outs: dict[int, set[str]] = {}
    rows = ReviewerOptOut.objects.filter(
        repository=repository,
        pr_number__in=pr_numbers,
        active=True,
    ).values_list("pr_number", "reviewer_login")
    for pr_number, reviewer_login in rows:
        opt_outs.setdefault(int(pr_number), set()).add(_normalize_login(reviewer_login))
    return opt_outs


def _active_proposal_rows(repository: Repository) -> list[tuple[int, str]]:
    """All active (``proposed``) assignment proposals for the repo as ``(pr_number, login)``.

    One repo-wide query feeds both proposal-aware builder additions (design doc 050): the set
    of PRs to withhold from re-proposal and the per-reviewer pending load. Login casing matches
    the snapshot / ``ReviewerProfile.github_login`` keying, so the load lookup lines up with the
    engine's exact-match capacity gate.
    """
    return [
        (int(pr_number), reviewer_login)
        for pr_number, reviewer_login in AssignmentProposal.objects.filter(
            repository=repository,
            state=AssignmentProposal.STATE_PROPOSED,
        ).values_list("pr_number", "reviewer_login")
    ]


def _pending_proposal_load(rows: Sequence[tuple[int, str]], *, weight: float) -> dict[str, float]:
    """Weighted pending-proposal load per reviewer login (one slot per active proposal)."""
    load: dict[str, float] = {}
    for _pr_number, reviewer_login in rows:
        load[reviewer_login] = load.get(reviewer_login, 0.0) + weight
    return load


def pending_proposal_load_for_repo(repository: Repository, *, weight: float) -> dict[str, float]:
    """Weighted pending-proposal load per reviewer login for a repo (public wrapper).

    Combines the active-proposal rows with per-login weighting so callers outside this module
    (e.g. ``analyzer.services.reviewer_load``) can fold pending-proposal load into capacity
    accounting without reaching into the private helpers. Data-driven: no active proposals -> ``{}``.
    Login casing matches ``ReviewerProfile.github_login`` / the snapshot keying.
    """
    return _pending_proposal_load(_active_proposal_rows(repository), weight=weight)


def _filter_prs_without_active_proposal(pr_numbers: Sequence[int], *, prs_with_active_proposal: Set[int]) -> list[int]:
    """Drop PRs that already have an active proposal.

    Enforces "one proposal per PR at a time": never re-propose a PR that is mid-proposal, and
    never propose it to a second reviewer while the first is deciding.
    """
    return [int(n) for n in pr_numbers if int(n) not in prs_with_active_proposal]


def _proposal_cooldowns_for_prs(
    repository: Repository,
    pr_numbers: Sequence[int],
    *,
    now: datetime,
    cooldown_days: int,
) -> dict[int, set[str]]:
    """Reviewers on soft cooldown per PR from a recently ``expired`` proposal (design doc 050).

    A silent timeout is soft: skip the reviewer for this PR for ``cooldown_days`` after the
    proposal expired, then let them be a candidate again. Unlike a decline (a permanent
    ``ReviewerOptOut``), this only advances the PR to other candidates now without foreclosing
    a reviewer who was merely away. Returns an ``excluded_by_pr`` fragment mergeable with the
    opt-out exclusions.
    """
    if not pr_numbers or cooldown_days <= 0:
        return {}
    cutoff = now - timedelta(days=cooldown_days)
    cooldowns: dict[int, set[str]] = {}
    rows = AssignmentProposal.objects.filter(
        repository=repository,
        pr_number__in=[int(n) for n in pr_numbers],
        state=AssignmentProposal.STATE_EXPIRED,
        decided_at__gte=cutoff,
    ).values_list("pr_number", "reviewer_login")
    for pr_number, reviewer_login in rows:
        cooldowns.setdefault(int(pr_number), set()).add(_normalize_login(reviewer_login))
    return cooldowns


def _merge_excluded_by_pr(*maps: dict[int, set[str]]) -> dict[int, set[str]]:
    """Union several ``{pr_number: {login, ...}}`` exclusion maps into one."""
    merged: dict[int, set[str]] = {}
    for mapping in maps:
        for pr_number, logins in mapping.items():
            merged.setdefault(int(pr_number), set()).update(logins)
    return merged


def _active_reviewer_logins(reviewers: Sequence[ReviewerProfile]) -> set[str]:
    return {
        _normalize_login(reviewer.github_login) for reviewer in reviewers if reviewer.auto_assign and not reviewer.temporary_break
    }


def _filter_prs_without_active_assignee(
    pr_numbers: Sequence[int],
    *,
    all_prs: Dict[int | str, dict],
    reviewers: Sequence[ReviewerProfile],
) -> list[int]:
    active_logins = _active_reviewer_logins(reviewers)
    filtered: list[int] = []
    for pr_number in pr_numbers:
        pr_entry = all_prs.get(pr_number) or all_prs.get(str(pr_number)) or {}
        assignees = {_normalize_login(str(login)) for login in (pr_entry.get("assignees") or []) if login}
        if assignees & active_logins:
            continue
        filtered.append(int(pr_number))
    return filtered


def _assignment_forbidden_labels(repository: Repository, *, rule_set: QueueRuleSet | None = None) -> Set[str]:
    """Resolve the repo's assignment-forbidden label set (normalized, lowercase).

    Defaults to the repository's canonical rule set when one is not supplied.
    """
    obj = rule_set or default_rule_set_for_repo(repository)
    if obj is None:
        return set()
    return set(rules_for_rule_set(obj).assignment_forbidden_labels or set())


def _filter_assignment_forbidden_prs(
    pr_numbers: Sequence[int],
    *,
    all_prs: Dict[int | str, dict],
    forbidden_labels: Set[str],
) -> list[int]:
    """Drop PRs carrying an assignment-forbidden label from the candidate pool.

    These PRs stay on the review queue (so the stale auto-unassign sweep still applies);
    they are only withheld from reviewer auto-assignment. Used for "post-review" labels
    such as ``maintainer-merge`` that a reviewer can take no further action on.
    """
    if not forbidden_labels:
        return [int(n) for n in pr_numbers]
    kept: list[int] = []
    for pr_number in pr_numbers:
        pr_entry = all_prs.get(pr_number) or all_prs.get(str(pr_number)) or {}
        label_names = {
            str(label.get("name")).strip().lower()
            for label in (pr_entry.get("labels") or [])
            if isinstance(label, dict) and label.get("name")
        }
        if label_names & forbidden_labels:
            continue
        kept.append(int(pr_number))
    return kept


def _queue_time_seconds(pr_entry: dict) -> tuple[float | None, DataStatus]:
    total_queue_time = pr_entry.get("total_queue_time") or {}
    status = str(total_queue_time.get("status") or "valid")
    value = total_queue_time.get("value_td")
    try:
        seconds = float(value) if value is not None else None
    except (TypeError, ValueError):
        seconds = None
    return seconds, status


def _is_light_color(hex_color: str) -> bool:
    color = hex_color.lstrip("#")
    if len(color) != 6:
        return False
    r = int(color[0:2], 16)
    g = int(color[2:4], 16)
    b = int(color[4:6], 16)
    a = 1 - (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return a < 0.5


@dataclass
class AssignmentStatistics:
    timestamp: datetime
    num_open: int
    assigned_open: list[int]
    number_multiple_assignees: int
    assignments: Dict[str, tuple[list[int], float, int]]


def build_reviewer_catalog(repository: Repository, *, now: datetime | None = None) -> list[ReviewerProfile]:
    """Hydrate reviewer profiles from ReviewerPreference rows.

    Carries both capacity gates: the concurrent ``maximum_capacity`` (stock) and the rolling-window
    ``max_new_assignments_per_week`` with its trailing intake count (flow, design doc 054). The
    count is fetched here, in one grouped query for the whole catalog, so that *every* consumer of a
    catalog — the nightly builder, the diagnostic trace, on-demand suggestions, and the reviewer-
    facing load line — sees the same figure. A reviewer whose push goes quiet then reads the number
    that silenced it, and the gate and the surfacing cannot drift apart.
    """
    current_time = now or datetime.now(timezone.utc)
    prefs = list(ReviewerPreference.objects.filter(repository=repository).select_related("user").order_by("user__github_login"))
    recent_counts = recent_assignment_counts(
        repository,
        [getattr(pref.user, "github_login", "") or "" for pref in prefs],
        window_days=assignment_rate_window_days(),
        now=current_time,
    )
    profiles: list[ReviewerProfile] = []
    for pref in prefs:
        login = getattr(pref.user, "github_login", None)
        if not login:
            continue
        temporary_break = bool(pref.away_until and pref.away_until > current_time)
        preferred_labels = list(pref.preferred_labels or [])
        conflicts = list(pref.conflict_of_interest or [])
        weekly_limit = pref.max_new_assignments_per_week
        profile = ReviewerProfile(
            github_login=login,
            maximum_capacity=pref.maximum_capacity,
            auto_assign=bool(pref.auto_assign),
            temporary_break=temporary_break,
            preferred_labels=preferred_labels,
            preferred_labels_lower={lab.lower() for lab in preferred_labels},
            free_form=pref.free_form or "",
            conflict_of_interest=conflicts,
            conflict_of_interest_lower={c.lower() for c in conflicts},
            weekly_limit=None if weekly_limit is None else int(weekly_limit),
            # Keyed by normalized login: the history column stores whatever casing the writing
            # caller used, so an unnormalized lookup here would read 0 for every reviewer whose
            # GitHub login is capitalized and quietly disable their limit (design doc 054).
            recent_assignment_count=recent_counts.get(_normalize_login(login), 0),
        )
        profiles.append(profile)
    return profiles


def collect_assignment_statistics(snapshot: dict) -> AssignmentStatistics:
    """Mirror legacy assignment statistics using snapshot payloads."""
    meta = snapshot.get("meta", {})
    generated_at = parse_datetime(meta.get("generated_at") or "") or datetime.now(timezone.utc)
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=timezone.utc)

    assignments: Dict[str, tuple[list[int], float, int]] = {}
    assigned_open_prs: list[int] = []
    prs: dict = snapshot.get("prs", {})

    for pr_number_raw, entry in prs.items():
        pr_number = int(pr_number_raw)
        assignees = entry.get("assignees") or []
        if not assignees:
            continue
        author = entry.get("author") or ""
        author_norm = _normalize_login(author)
        for login in assignees:
            current = assignments.get(login, ([], 0.0, 0))
            open_list = list(current[0])
            weighted_load = float(current[1])
            total_assigned = int(current[2])

            open_list.append(pr_number)
            if _normalize_login(login) != author_norm:
                weighted_load += _compute_weight(pr_number, entry)
            total_assigned += 1

            assignments[login] = (open_list, weighted_load, total_assigned)
            assigned_open_prs.append(pr_number)

    assigned_unique = sorted(set(assigned_open_prs))
    number_multiple_assignees = len(assigned_open_prs) - len(assigned_unique)

    return AssignmentStatistics(
        timestamp=generated_at,
        num_open=len(prs),
        assigned_open=assigned_unique,
        number_multiple_assignees=number_multiple_assignees,
        assignments=assignments,
    )


def _compute_weight(pr_number: int, pr_entry: dict) -> float:
    pr_status_value = pr_entry.get("pr_status")
    try:
        status = PRStatus(pr_status_value)
    except ValueError:
        return 0.0

    if status in (PRStatus.AwaitingReview, PRStatus.MergeConflict):
        return 1.0
    if status in (
        PRStatus.Blocked,
        PRStatus.Delegated,
        PRStatus.AwaitingBors,
        PRStatus.Closed,
        PRStatus.Contradictory,
        PRStatus.NotReady,
        PRStatus.HelpWanted,
    ):
        return 0.0
    if status in (PRStatus.AwaitingAuthor, PRStatus.AwaitingDecision):
        last_status_change = pr_entry.get("last_status_change")
        if not isinstance(last_status_change, dict):
            return 0.1
        data_status = str(last_status_change.get("status") or "").lower()
        if data_status in {"missing", "incomplete"}:
            return 0.1
        delta = last_status_change.get("delta") or {}
        days = delta.get("days")
        try:
            days_int = int(days)
        except (TypeError, ValueError):
            return 0.1
        return 1 / (days_int + 1)
    return 0.0


def suggest_reviewers_many(
    *,
    reviewers: Sequence[ReviewerProfile],
    assignments: Dict[str, tuple[list[int], float, int]],
    prs_to_assign: Sequence[int],
    all_prs: Dict[int | str, dict],
    rng: random.Random | None = None,
    excluded_by_pr: dict[int, set[str]] | None = None,
    priority_scorer: PRAssignmentPriorityScorer | None = None,
    topic_label_matcher: TopicLabelMatcher = default_topic_label_matcher,
) -> dict[int, str]:
    """Suggest reviewers for many PRs using the pure assignment engine."""
    result = run_assignment_simulation(
        inputs=SimulationInputs(
            reviewers=reviewers,
            assignments=assignments,
            prs_to_assign=prs_to_assign,
            all_prs=all_prs,
            excluded_by_pr=excluded_by_pr,
            topic_label_matcher=topic_label_matcher,
        ),
        rng=rng,
        priority_scorer=priority_scorer,
        include_trace=False,
    )
    return result.suggestions


def suggest_reviewers_many_with_trace(
    *,
    reviewers: Sequence[ReviewerProfile],
    assignments: Dict[str, tuple[list[int], float, int]],
    prs_to_assign: Sequence[int],
    all_prs: Dict[int | str, dict],
    rng: random.Random | None = None,
    excluded_by_pr: dict[int, set[str]] | None = None,
    priority_scorer: PRAssignmentPriorityScorer | None = None,
    topic_label_matcher: TopicLabelMatcher = default_topic_label_matcher,
) -> tuple[dict[int, str], dict[str, dict]]:
    """Suggest reviewers for many PRs and return the compact per-PR trace."""
    result = run_assignment_simulation(
        inputs=SimulationInputs(
            reviewers=reviewers,
            assignments=assignments,
            prs_to_assign=prs_to_assign,
            all_prs=all_prs,
            excluded_by_pr=excluded_by_pr,
            topic_label_matcher=topic_label_matcher,
        ),
        rng=rng,
        priority_scorer=priority_scorer,
        include_trace=True,
    )
    return result.suggestions, result.per_pr


@dataclass
class AssignmentInputs:
    """Proposal-aware candidate/load inputs shared by the builder, the trace, and suggestions."""

    reviewers: list[ReviewerProfile]
    assignments: Dict[str, tuple[list[int], float, int]]
    queue_prs: list[int]
    assignable_queue_prs: list[int]
    excluded_by_pr: dict[int, set[str]]


def prepare_assignment_inputs(
    repository: Repository,
    *,
    payload: dict,
    now: datetime,
    rule_set: QueueRuleSet | None,
) -> AssignmentInputs:
    """Assemble the candidate pool, reviewer load, and exclusions for a build.

    Beyond the legacy filters (active-assignee / assignment-forbidden labels / opt-outs) this
    folds in the three proposal-aware additions from design doc 050 so the builder and the
    diagnostic trace agree: pending proposals contribute weighted load, PRs with an active
    proposal are withheld from re-proposal, and reviewers with a recently expired proposal for a
    PR are excluded for it (soft cooldown, merged into the per-PR exclusion set alongside
    opt-outs).

    Public because on-demand assignment suggestions (design doc 053, Invariant 5) must share
    this exact pool: any candidate filter added elsewhere would let suggestions offer PRs the
    nightly builder refuses. New exclusion rules belong here, not at a call site.
    """
    reviewers = build_reviewer_catalog(repository, now=now)
    assignment_stats = collect_assignment_statistics(payload)

    proposal_weight = float(getattr(settings, "ANALYZER_ASSIGNMENT_PROPOSAL_PENDING_LOAD_WEIGHT", 1.0))
    active_proposal_rows = _active_proposal_rows(repository)
    prs_with_active_proposal = {pr_number for pr_number, _ in active_proposal_rows}
    assignments = add_pending_proposal_load(
        assignment_stats.assignments,
        _pending_proposal_load(active_proposal_rows, weight=proposal_weight),
    )

    all_prs = payload.get("prs", {})
    dashboards = payload.get("lists", {}).get("dashboards", {})
    queue_prs = list(dashboards.get("Queue", []))
    assignable_queue_prs = _filter_prs_without_active_assignee(queue_prs, all_prs=all_prs, reviewers=reviewers)
    assignable_queue_prs = _filter_assignment_forbidden_prs(
        assignable_queue_prs,
        all_prs=all_prs,
        forbidden_labels=_assignment_forbidden_labels(repository, rule_set=rule_set),
    )
    assignable_queue_prs = _filter_prs_without_active_proposal(
        assignable_queue_prs, prs_with_active_proposal=prs_with_active_proposal
    )

    cooldown_days = int(getattr(settings, "ANALYZER_ASSIGNMENT_PROPOSAL_EXPIRE_COOLDOWN_DAYS", 14))
    excluded_by_pr = _merge_excluded_by_pr(
        _opt_outs_for_prs(repository, assignable_queue_prs),
        _proposal_cooldowns_for_prs(repository, assignable_queue_prs, now=now, cooldown_days=cooldown_days),
    )
    return AssignmentInputs(
        reviewers=reviewers,
        assignments=assignments,
        queue_prs=queue_prs,
        assignable_queue_prs=assignable_queue_prs,
        excluded_by_pr=excluded_by_pr,
    )


def build_reviewer_assignment_trace(
    repository: Repository,
    *,
    queue_snapshot: QueueSnapshot,
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> dict:
    current_time = now or datetime.now(timezone.utc)
    payload = queue_snapshot.payload
    topic_label_matcher = topic_label_matcher_for_repo(repository)
    inputs = prepare_assignment_inputs(repository, payload=payload, now=current_time, rule_set=None)
    queue_prs = inputs.queue_prs
    assignable_queue_prs = inputs.assignable_queue_prs

    suggestions, per_pr = suggest_reviewers_many_with_trace(
        reviewers=inputs.reviewers,
        assignments=inputs.assignments,
        prs_to_assign=assignable_queue_prs,
        all_prs=payload.get("prs", {}),
        rng=rng,
        excluded_by_pr=inputs.excluded_by_pr,
        topic_label_matcher=topic_label_matcher,
    )

    reason_counts: dict[str, int] = {}
    for trace in per_pr.values():
        reason = trace.get("reason")
        if reason:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1

    meta = {
        "schema_version": "v1-trace-draft",
        "generated_at": _isoformat(current_time),
        "repository": f"{repository.owner}/{repository.name}",
        "rule_set_id": payload.get("meta", {}).get("rule_set_id", "default"),
        "queue_snapshot_generated_at": payload.get("meta", {}).get("generated_at"),
        "queue_snapshot_cache_key": queue_snapshot.cache_key,
        "queue_prs": len(queue_prs),
        "assignment_candidate_prs": len(assignable_queue_prs),
    }
    summary = {
        "attempted": len(assignable_queue_prs),
        "assigned": len(suggestions),
        "unassigned": len(assignable_queue_prs) - len(suggestions),
        "reason_counts": reason_counts,
    }

    return {"meta": meta, "summary": summary, "per_pr": per_pr}


def compute_area_stats(
    *,
    existing_assignments: Dict[str, tuple[list[int], float, int]],
    reviewers: Sequence[ReviewerProfile],
    queue_pr_numbers: Sequence[int],
    all_prs: Dict[int | str, dict],
    rng: random.Random | None = None,
    topic_label_matcher: TopicLabelMatcher = default_topic_label_matcher,
) -> dict[str, dict]:
    """Compute area-level metrics for queued PRs."""
    area_data: dict[str, dict] = {}
    reviewer_logins_lower = {_normalize_login(r.github_login) for r in reviewers}

    for pr_number in queue_pr_numbers:
        pr_entry = all_prs.get(pr_number) or all_prs.get(str(pr_number))
        if not pr_entry:
            continue
        topic_labels = [lab for lab in pr_entry.get("labels") or [] if topic_label_matcher(lab.get("name"))]
        assignees_lower = {_normalize_login(a) for a in pr_entry.get("assignees") or []}
        seconds, queue_status = _queue_time_seconds(pr_entry)
        missing_queue_time = queue_status != "valid" or seconds is None

        for label in topic_labels:
            name = label.get("name")
            if not name:
                continue
            data = area_data.setdefault(name, {})
            color = label.get("color")
            if color and "bgcolor" not in data:
                data["bgcolor"] = color
                data["fgcolor"] = "000000" if _is_light_color(color) else "FFFFFF"

            data["total_queue_time"] = data.get("total_queue_time", 0) + (seconds or 0)
            data["_missing_queue_time"] = data.get("_missing_queue_time", 0) + (1 if missing_queue_time else 0)

            if assignees_lower & reviewer_logins_lower:
                data["assigned"] = data.get("assigned", 0) + 1
                data["assigned_queue_time"] = data.get("assigned_queue_time", 0) + (seconds or 0)
                data["_missing_assigned_queue_time"] = data.get("_missing_assigned_queue_time", 0) + (
                    1 if missing_queue_time else 0
                )
            else:
                data["unassigned"] = data.get("unassigned", 0) + 1

            data["on_queue"] = data.get("on_queue", 0) + 1

    for reviewer in reviewers:
        for area in reviewer.preferred_labels:
            data = area_data.setdefault(area, {})
            data["num_reviewers"] = data.get("num_reviewers", 0) + 1
            if reviewer.auto_assign and not reviewer.temporary_break:
                data["num_reviewers_on_rotation"] = data.get("num_reviewers_on_rotation", 0) + 1

    for label_name, data in area_data.items():
        availability = suggest_reviewer_for_pr(
            pr_number=-1,
            pr_entry={"labels": [{"name": label_name}], "author": ""},
            reviewers=reviewers,
            assignment_stats=existing_assignments,
            rng=rng,
            topic_label_matcher=topic_label_matcher,
        )
        data["at_max_capacity"] = availability.suggested is None

        missing_total = data.pop("_missing_queue_time", 0)
        missing_assigned = data.pop("_missing_assigned_queue_time", 0)
        on_queue = data.get("on_queue", 0)
        assigned = data.get("assigned", 0)

        if on_queue > missing_total:
            data["avg_queue_time"] = data["total_queue_time"] / (on_queue - missing_total)

        if assigned:
            data["ratio"] = on_queue / assigned
            if assigned > missing_assigned:
                data["avg_assigned_queue_time"] = data["assigned_queue_time"] / (assigned - missing_assigned)
        else:
            data["ratio"] = None

    return area_data


@dataclass
class ReviewerAssignmentBuilder:
    """Build reviewer assignment payloads anchored to a queue snapshot."""

    rng: random.Random | None = None
    queue_snapshot_builder: QueueboardSnapshotBuilder = field(default_factory=QueueboardSnapshotBuilder)

    def build(
        self,
        repository: Repository,
        *,
        queue_snapshot: QueueSnapshot | None = None,
        cache_key: str | None = None,
        now: datetime | None = None,
        rule_set: QueueRuleSet | None = None,
    ) -> dict:
        current_time = now or datetime.now(timezone.utc)
        queue_obj = queue_snapshot or self._get_or_build_queue_snapshot(repository, cache_key=cache_key, rule_set=rule_set)
        payload = queue_obj.payload

        inputs = prepare_assignment_inputs(repository, payload=payload, now=current_time, rule_set=rule_set)

        automatic_assignments = suggest_reviewers_many(
            reviewers=inputs.reviewers,
            assignments=inputs.assignments,
            prs_to_assign=inputs.assignable_queue_prs,
            all_prs=payload.get("prs", {}),
            rng=self.rng,
            excluded_by_pr=inputs.excluded_by_pr,
            topic_label_matcher=topic_label_matcher_for_repo(repository),
        )

        rule_set_id = payload.get("meta", {}).get("rule_set_id", "default")
        meta = {
            "schema_version": "v1-draft",
            "generated_at": _isoformat(current_time),
            "repository": f"{repository.owner}/{repository.name}",
            "rule_set_id": rule_set_id,
            "queue_snapshot_generated_at": payload.get("meta", {}).get("generated_at"),
            "queue_snapshot_cache_key": queue_obj.cache_key,
        }

        return {"meta": meta, "automatic_assignments": automatic_assignments}

    def build_and_store(
        self,
        repository: Repository,
        *,
        queue_snapshot: QueueSnapshot | None = None,
        cache_key: str | None = None,
        expires_at: datetime | None = None,
        now: datetime | None = None,
        rule_set: QueueRuleSet | None = None,
    ) -> ReviewerAssignmentSnapshot:
        current_time = now or datetime.now(timezone.utc)
        queue_obj = queue_snapshot or self._get_or_build_queue_snapshot(repository, cache_key=cache_key, rule_set=rule_set)
        payload = self.build(repository, queue_snapshot=queue_obj, now=current_time, rule_set=rule_set)

        etag = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        key = cache_key or queue_obj.cache_key
        assignment_count = len(payload.get("automatic_assignments", {}))

        obj, _ = ReviewerAssignmentSnapshot.objects.update_or_create(
            repository=repository,
            cache_key=key,
            defaults={
                "queue_snapshot": queue_obj,
                "generated_at": current_time,
                "payload": payload,
                "etag": etag,
                "assignment_count": assignment_count,
                "expires_at": expires_at,
            },
        )
        return obj

    def _get_or_build_queue_snapshot(
        self, repository: Repository, *, cache_key: str | None, rule_set: QueueRuleSet | None
    ) -> QueueSnapshot:
        key = cache_key or (str(rule_set.id) if rule_set else "default")
        existing = QueueSnapshot.objects.filter(repository=repository, cache_key=key).order_by("-generated_at").first()
        if existing:
            return existing
        return self.queue_snapshot_builder.build_and_store(repository, cache_key=key, rule_set=rule_set)


@dataclass
class AreaStatsBuilder:
    """Build area stats payloads anchored to a queue snapshot."""

    rng: random.Random | None = None
    queue_snapshot_builder: QueueboardSnapshotBuilder = field(default_factory=QueueboardSnapshotBuilder)

    def build(
        self,
        repository: Repository,
        *,
        queue_snapshot: QueueSnapshot | None = None,
        cache_key: str | None = None,
        now: datetime | None = None,
        rule_set: QueueRuleSet | None = None,
    ) -> dict:
        current_time = now or datetime.now(timezone.utc)
        queue_obj = queue_snapshot or self._get_or_build_queue_snapshot(repository, cache_key=cache_key, rule_set=rule_set)
        payload = queue_obj.payload

        reviewers = build_reviewer_catalog(repository, now=current_time)
        assignment_stats = collect_assignment_statistics(payload)
        # Pending proposals occupy capacity (design doc 050), for area stats exactly as for the
        # assignment builder/trace — otherwise at_max_capacity here disagrees with what the next
        # assignment run will actually do.
        proposal_weight = float(getattr(settings, "ANALYZER_ASSIGNMENT_PROPOSAL_PENDING_LOAD_WEIGHT", 1.0))
        existing_assignments = add_pending_proposal_load(
            assignment_stats.assignments,
            _pending_proposal_load(_active_proposal_rows(repository), weight=proposal_weight),
        )
        dashboards = payload.get("lists", {}).get("dashboards", {})
        queue_prs = dashboards.get("Queue", [])

        area_stats = compute_area_stats(
            existing_assignments=existing_assignments,
            reviewers=reviewers,
            queue_pr_numbers=queue_prs,
            all_prs=payload.get("prs", {}),
            rng=self.rng,
            topic_label_matcher=topic_label_matcher_for_repo(repository),
        )

        rule_set_id = payload.get("meta", {}).get("rule_set_id", "default")
        meta = {
            "schema_version": "v1-draft",
            "generated_at": _isoformat(current_time),
            "repository": f"{repository.owner}/{repository.name}",
            "rule_set_id": rule_set_id,
            "queue_snapshot_generated_at": payload.get("meta", {}).get("generated_at"),
            "queue_snapshot_cache_key": queue_obj.cache_key,
        }

        return {"meta": meta, "area_stats": area_stats}

    def build_and_store(
        self,
        repository: Repository,
        *,
        queue_snapshot: QueueSnapshot | None = None,
        cache_key: str | None = None,
        expires_at: datetime | None = None,
        now: datetime | None = None,
        rule_set: QueueRuleSet | None = None,
    ) -> AreaStatsSnapshot:
        current_time = now or datetime.now(timezone.utc)
        queue_obj = queue_snapshot or self._get_or_build_queue_snapshot(repository, cache_key=cache_key, rule_set=rule_set)
        payload = self.build(repository, queue_snapshot=queue_obj, now=current_time, rule_set=rule_set)

        etag = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        key = cache_key or queue_obj.cache_key
        area_count = len(payload.get("area_stats", {}))

        obj, _ = AreaStatsSnapshot.objects.update_or_create(
            repository=repository,
            cache_key=key,
            defaults={
                "queue_snapshot": queue_obj,
                "generated_at": current_time,
                "payload": payload,
                "etag": etag,
                "area_count": area_count,
                "expires_at": expires_at,
            },
        )
        return obj

    def _get_or_build_queue_snapshot(
        self, repository: Repository, *, cache_key: str | None, rule_set: QueueRuleSet | None
    ) -> QueueSnapshot:
        key = cache_key or (str(rule_set.id) if rule_set else "default")
        existing = QueueSnapshot.objects.filter(repository=repository, cache_key=key).order_by("-generated_at").first()
        if existing:
            return existing
        return self.queue_snapshot_builder.build_and_store(repository, cache_key=key, rule_set=rule_set)


__all__ = [
    "AssignmentInputs",
    "AssignmentStatistics",
    "AreaStatsBuilder",
    "PRAssignmentPriority",
    "PRAssignmentPriorityScorer",
    "ReviewerAssignmentBuilder",
    "ReviewerProfile",
    "ReviewerSuggestionResult",
    "add_pending_proposal_load",
    "build_reviewer_assignment_trace",
    "build_reviewer_catalog",
    "collect_assignment_statistics",
    "compute_area_stats",
    "pending_proposal_load_for_repo",
    "prepare_assignment_inputs",
    "rank_prs_for_assignment",
    "suggest_reviewer_for_pr",
    "suggest_reviewers_many",
    "suggest_reviewers_many_with_trace",
]
