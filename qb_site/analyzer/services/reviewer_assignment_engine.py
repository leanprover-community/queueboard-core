from __future__ import annotations

import random
from dataclasses import dataclass, field
import re
from typing import Callable, Dict, Iterable, Sequence


@dataclass(frozen=True)
class ReviewerProfile:
    github_login: str
    maximum_capacity: int
    auto_assign: bool
    temporary_break: bool
    preferred_labels: list[str]
    preferred_labels_lower: set[str]
    free_form: str
    conflict_of_interest: list[str]
    conflict_of_interest_lower: set[str]


@dataclass
class ReviewerSuggestionResult:
    suggested: str | None
    all_potential_reviewers: list[str]
    all_available_reviewers: list[str]
    reason: str | None = None


@dataclass(frozen=True)
class PRAssignmentPriority:
    sort_key: tuple[object, ...] = ()
    details: dict[str, object] = field(default_factory=dict)


PRAssignmentPriorityScorer = Callable[
    [int, dict, Sequence[ReviewerProfile], Dict[str, tuple[list[int], float, int]], set[str]],
    PRAssignmentPriority,
]


@dataclass(frozen=True)
class SimulationInputs:
    reviewers: Sequence[ReviewerProfile]
    assignments: Dict[str, tuple[list[int], float, int]]
    prs_to_assign: Iterable[int]
    all_prs: Dict[int | str, dict]
    excluded_by_pr: dict[int, set[str]] | None = None


@dataclass(frozen=True)
class SimulationResult:
    suggestions: dict[int, str]
    per_pr: dict[str, dict]
    final_assignment_stats: Dict[str, tuple[list[int], float, int]]


def _normalize_login(login: str | None) -> str:
    return (login or "").strip().lower()


def _is_topic_label(name: str | None) -> bool:
    if not name:
        return False
    lowered = name.lower()
    return lowered.startswith("t-") or lowered in {"ci", "imo", "tech debt"}


def _topic_labels(pr_entry: dict) -> list[str]:
    labels = pr_entry.get("labels") or []
    names: list[str] = []
    for label in labels:
        name = label.get("name")
        if _is_topic_label(name):
            names.append(name)
    return names


def _queue_age_seconds(pr_entry: dict) -> float | None:
    total_queue_time = pr_entry.get("total_queue_time") or {}
    status = str(total_queue_time.get("status") or "valid").lower()
    if status != "valid":
        return None
    value = total_queue_time.get("value_td")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _title_priority(title: str | None) -> int:
    normalized = (title or "").strip().lower()
    return 0 if re.match(r"^feat(?:[:( ]|$)", normalized) else 1


def _current_weight(login: str, assignments: Dict[str, tuple[list[int], float, int]]) -> float:
    data = assignments.get(login)
    return float(data[1]) if data else 0.0


def _reviewer_candidate_state(
    *,
    pr_entry: dict,
    reviewers: Sequence[ReviewerProfile],
    assignment_stats: Dict[str, tuple[list[int], float, int]],
    excluded_logins: set[str] | None = None,
) -> tuple[list[str], list[str], list[float], str | None]:
    labels = _topic_labels(pr_entry)
    labels_lower = [lab.lower() for lab in labels]
    if not labels_lower:
        return [], [], [], "missing-topic-label"

    author = pr_entry.get("author") or ""
    author_norm = _normalize_login(author)

    matching: list[tuple[ReviewerProfile, list[str]]] = []
    excluded_lower = {_normalize_login(login) for login in (excluded_logins or set()) if login}
    if labels_lower:
        for reviewer in reviewers:
            if author_norm in {reviewer.github_login.lower(), *reviewer.conflict_of_interest_lower}:
                continue
            match = [lab for lab in labels_lower if lab in reviewer.preferred_labels_lower]
            if match:
                matching.append((reviewer, match))
    else:
        matching = [(rev, []) for rev in reviewers if _normalize_login(rev.github_login) != author_norm]

    if not matching:
        return [], [], [], "no-match"

    if not labels_lower:
        proposed = matching
    else:
        max_score = max(len(m) for _, m in matching)
        if max_score > 1:
            proposed = [(rev, m) for rev, m in matching if len(m) == max_score]
        else:
            proposed = [(rev, m) for rev, m in matching if m]
        if not proposed:
            return [], [], [], "no-matching-labels"

    proposed_sorted = sorted(proposed, key=lambda item: _current_weight(item[0].github_login, assignment_stats))
    all_potential = [rev.github_login for rev, _ in proposed_sorted]

    available: list[str] = []
    available_weights: list[float] = []
    for reviewer, _ in proposed_sorted:
        if _normalize_login(reviewer.github_login) in excluded_lower:
            continue
        current_weight = _current_weight(reviewer.github_login, assignment_stats)
        remaining = reviewer.maximum_capacity - current_weight
        if remaining > 0 and reviewer.auto_assign and not reviewer.temporary_break:
            available.append(reviewer.github_login)
            available_weights.append(remaining)

    return all_potential, available, available_weights, None


def _default_pr_assignment_priority(
    pr_number: int,
    pr_entry: dict,
    reviewers: Sequence[ReviewerProfile],
    assignment_stats: Dict[str, tuple[list[int], float, int]],
    excluded_logins: set[str],
) -> PRAssignmentPriority:
    _, available, available_weights, _ = _reviewer_candidate_state(
        pr_entry=pr_entry,
        reviewers=reviewers,
        assignment_stats=assignment_stats,
        excluded_logins=excluded_logins,
    )
    queue_age_seconds = _queue_age_seconds(pr_entry)
    title_priority = _title_priority(pr_entry.get("title"))
    available_reviewer_count = len(available)
    total_remaining_capacity = float(sum(available_weights))
    assignable_now = available_reviewer_count > 0
    queue_age_sort = -(queue_age_seconds if queue_age_seconds is not None else 0.0)
    priority = PRAssignmentPriority(
        sort_key=(
            0 if assignable_now else 1,
            available_reviewer_count,
            total_remaining_capacity,
            queue_age_sort,
            title_priority,
            pr_number,
        ),
        details={
            "assignable_now": assignable_now,
            "has_topic_label": bool(_topic_labels(pr_entry)),
            "available_reviewer_count": available_reviewer_count,
            "total_remaining_capacity": total_remaining_capacity,
            "queue_age_seconds": queue_age_seconds,
            "title_priority": title_priority,
        },
    )
    return priority


def rank_prs_for_assignment(
    *,
    prs_to_assign: Iterable[int],
    all_prs: Dict[int | str, dict],
    reviewers: Sequence[ReviewerProfile],
    assignment_stats: Dict[str, tuple[list[int], float, int]],
    excluded_by_pr: dict[int, set[str]] | None = None,
    priority_scorer: PRAssignmentPriorityScorer | None = None,
) -> tuple[list[int], dict[str, dict]]:
    scorer = priority_scorer or _default_pr_assignment_priority
    ranked_items: list[tuple[tuple[object, ...], int, int]] = []
    trace: dict[str, dict] = {}

    for input_index, pr_number in enumerate(prs_to_assign):
        pr_entry = all_prs.get(pr_number) or all_prs.get(str(pr_number))
        if not pr_entry:
            trace[str(pr_number)] = {"input_index": input_index, "missing_pr": True}
            ranked_items.append(((), input_index, pr_number))
            continue

        excluded_logins = excluded_by_pr.get(pr_number, set()) if excluded_by_pr else set()
        priority = scorer(pr_number, pr_entry, reviewers, assignment_stats, excluded_logins)
        trace[str(pr_number)] = {
            "input_index": input_index,
            "sort_key": list(priority.sort_key),
            "details": priority.details,
        }
        ranked_items.append((priority.sort_key, input_index, pr_number))

    ranked_items.sort(key=lambda item: (item[0], item[1]))
    ordered_prs = [pr_number for _, _, pr_number in ranked_items]

    for output_index, pr_number in enumerate(ordered_prs):
        trace.setdefault(str(pr_number), {})
        trace[str(pr_number)]["output_index"] = output_index

    return ordered_prs, trace


def suggest_reviewer_for_pr(
    *,
    pr_number: int,
    pr_entry: dict,
    reviewers: Sequence[ReviewerProfile],
    assignment_stats: Dict[str, tuple[list[int], float, int]],
    rng: random.Random | None = None,
    excluded_logins: set[str] | None = None,
) -> ReviewerSuggestionResult:
    all_potential, available, available_weights, unavailable_reason = _reviewer_candidate_state(
        pr_entry=pr_entry,
        reviewers=reviewers,
        assignment_stats=assignment_stats,
        excluded_logins=excluded_logins,
    )
    if not all_potential:
        return ReviewerSuggestionResult(
            suggested=None,
            all_potential_reviewers=[],
            all_available_reviewers=[],
            reason=unavailable_reason or "no-match",
        )

    if not available:
        return ReviewerSuggestionResult(
            suggested=None, all_potential_reviewers=all_potential, all_available_reviewers=[], reason="no-capacity"
        )

    picker = rng if rng is not None else random
    choose = getattr(picker, "choices", random.choices)
    suggested = choose(available, weights=available_weights, k=1)[0]

    return ReviewerSuggestionResult(
        suggested=suggested,
        all_potential_reviewers=all_potential,
        all_available_reviewers=available,
        reason=None,
    )


def _pr_trace_base(pr_entry: dict, *, excluded_logins: set[str]) -> dict:
    return {
        "labels": _topic_labels(pr_entry),
        "author": pr_entry.get("author") or "",
        "opt_outs": sorted(login for login in excluded_logins if login),
    }


def suggest_reviewer_for_pr_with_trace(
    *,
    pr_entry: dict,
    reviewers: Sequence[ReviewerProfile],
    assignment_stats: Dict[str, tuple[list[int], float, int]],
    rng: random.Random | None = None,
    excluded_logins: set[str] | None = None,
) -> tuple[ReviewerSuggestionResult, dict]:
    labels = _topic_labels(pr_entry)
    labels_lower = [lab.lower() for lab in labels]
    if not labels_lower:
        excluded_lower = {_normalize_login(login) for login in (excluded_logins or set()) if login}
        trace = _pr_trace_base(pr_entry, excluded_logins=excluded_lower)
        trace["candidate_counts"] = {"matching_label": 0, "after_exclusions": 0, "available_capacity": 0}
        result = ReviewerSuggestionResult(
            suggested=None,
            all_potential_reviewers=[],
            all_available_reviewers=[],
            reason="missing-topic-label",
        )
        trace.update({"available": [], "picked": None, "reason": result.reason})
        return result, trace

    author = pr_entry.get("author") or ""
    author_norm = _normalize_login(author)

    excluded_lower = {_normalize_login(login) for login in (excluded_logins or set()) if login}
    trace: dict = _pr_trace_base(pr_entry, excluded_logins=excluded_lower)
    filtered: dict[str, list[str]] = {
        "conflict_of_interest": [],
        "opt_out": [],
        "temporary_break": [],
        "auto_assign_disabled": [],
        "at_capacity": [],
    }

    matching: list[tuple[ReviewerProfile, list[str]]] = []
    if labels_lower:
        for reviewer in reviewers:
            reviewer_login = reviewer.github_login
            if author_norm in {reviewer_login.lower(), *reviewer.conflict_of_interest_lower}:
                filtered["conflict_of_interest"].append(reviewer_login)
                continue
            match = [lab for lab in labels_lower if lab in reviewer.preferred_labels_lower]
            if match:
                matching.append((reviewer, match))
    else:
        for reviewer in reviewers:
            reviewer_login = reviewer.github_login
            if author_norm in {reviewer_login.lower(), *reviewer.conflict_of_interest_lower}:
                filtered["conflict_of_interest"].append(reviewer_login)
                continue
            matching.append((reviewer, []))

    if not matching:
        trace["candidate_counts"] = {"matching_label": 0, "after_exclusions": 0, "available_capacity": 0}
        trace["filtered"] = {k: sorted(set(v)) for k, v in filtered.items() if v}
        result = ReviewerSuggestionResult(
            suggested=None, all_potential_reviewers=[], all_available_reviewers=[], reason="no-match"
        )
        trace.update({"available": [], "picked": None, "reason": result.reason})
        return result, trace

    if not labels_lower:
        proposed = matching
    else:
        max_score = max(len(m) for _, m in matching)
        if max_score > 1:
            proposed = [(rev, m) for rev, m in matching if len(m) == max_score]
        else:
            proposed = [(rev, m) for rev, m in matching if m]
        if not proposed:
            trace["candidate_counts"] = {
                "matching_label": len(matching),
                "after_exclusions": 0,
                "available_capacity": 0,
            }
            trace["filtered"] = {k: sorted(set(v)) for k, v in filtered.items() if v}
            result = ReviewerSuggestionResult(
                suggested=None,
                all_potential_reviewers=[],
                all_available_reviewers=[],
                reason="no-matching-labels",
            )
            trace.update({"available": [], "picked": None, "reason": result.reason})
            return result, trace

    proposed_sorted = sorted(proposed, key=lambda item: _current_weight(item[0].github_login, assignment_stats))
    all_potential = [rev.github_login for rev, _ in proposed_sorted]

    available: list[str] = []
    weights: dict[str, dict[str, float]] = {}
    for reviewer, _ in proposed_sorted:
        reviewer_login = reviewer.github_login
        if _normalize_login(reviewer_login) in excluded_lower:
            filtered["opt_out"].append(reviewer_login)
            continue

        current_weight = _current_weight(reviewer_login, assignment_stats)
        remaining = reviewer.maximum_capacity - current_weight

        if remaining <= 0:
            filtered["at_capacity"].append(reviewer_login)
        if not reviewer.auto_assign:
            filtered["auto_assign_disabled"].append(reviewer_login)
        if reviewer.temporary_break:
            filtered["temporary_break"].append(reviewer_login)

        if remaining > 0 and reviewer.auto_assign and not reviewer.temporary_break:
            available.append(reviewer_login)
            weights[reviewer_login] = {
                "current_weight": float(current_weight),
                "remaining_capacity": float(remaining),
            }

    if not available:
        result = ReviewerSuggestionResult(
            suggested=None, all_potential_reviewers=all_potential, all_available_reviewers=[], reason="no-capacity"
        )
        trace.update(
            {
                "candidate_counts": {
                    "matching_label": len(matching) if labels_lower else 0,
                    "after_exclusions": len(all_potential),
                    "available_capacity": 0,
                },
                "potential": all_potential,
                "available": [],
                "weights": weights,
                "filtered": {k: sorted(set(v)) for k, v in filtered.items() if v},
                "picked": None,
                "reason": result.reason,
            }
        )
        return result, trace

    picker = rng if rng is not None else random
    choose = getattr(picker, "choices", random.choices)
    suggested = choose(available, weights=[weights[login]["remaining_capacity"] for login in available], k=1)[0]

    result = ReviewerSuggestionResult(
        suggested=suggested,
        all_potential_reviewers=all_potential,
        all_available_reviewers=available,
        reason=None,
    )
    trace.update(
        {
            "candidate_counts": {
                "matching_label": len(matching) if labels_lower else 0,
                "after_exclusions": len(all_potential),
                "available_capacity": len(available),
            },
            "potential": all_potential,
            "available": available,
            "weights": weights,
            "filtered": {k: sorted(set(v)) for k, v in filtered.items() if v},
            "picked": suggested,
            "reason": None,
        }
    )
    return result, trace


def run_assignment_simulation(
    *,
    inputs: SimulationInputs,
    rng: random.Random | None = None,
    priority_scorer: PRAssignmentPriorityScorer | None = None,
    include_trace: bool = False,
) -> SimulationResult:
    stats_copy: Dict[str, tuple[list[int], float, int]] = {
        login: (list(open_list), float(weight), int(total)) for login, (open_list, weight, total) in inputs.assignments.items()
    }
    suggestions: dict[int, str] = {}
    remaining_prs = list(inputs.prs_to_assign)

    per_pr: dict[str, dict]
    if include_trace:
        per_pr = {}
    else:
        per_pr = {}

    round_index = 0
    while remaining_prs:
        ordered_prs, ranking_trace = rank_prs_for_assignment(
            prs_to_assign=remaining_prs,
            all_prs=inputs.all_prs,
            reviewers=inputs.reviewers,
            assignment_stats=stats_copy,
            excluded_by_pr=inputs.excluded_by_pr,
            priority_scorer=priority_scorer,
        )
        pr_number = ordered_prs[0]

        if include_trace:
            per_pr.setdefault(str(pr_number), {})
            per_pr[str(pr_number)]["round_index"] = round_index
            per_pr[str(pr_number)]["priority"] = ranking_trace[str(pr_number)]

        pr_entry = inputs.all_prs.get(pr_number) or inputs.all_prs.get(str(pr_number))
        if not pr_entry:
            if include_trace:
                per_pr[str(pr_number)].update(
                    {
                        "labels": [],
                        "author": "",
                        "opt_outs": [],
                        "candidate_counts": {"matching_label": 0, "after_exclusions": 0, "available_capacity": 0},
                        "available": [],
                        "picked": None,
                        "reason": "missing-pr",
                    }
                )
            remaining_prs.remove(pr_number)
            round_index += 1
            continue

        excluded_logins = inputs.excluded_by_pr.get(pr_number) if inputs.excluded_by_pr else None
        if include_trace:
            result, trace = suggest_reviewer_for_pr_with_trace(
                pr_entry=pr_entry,
                reviewers=inputs.reviewers,
                assignment_stats=stats_copy,
                rng=rng,
                excluded_logins=excluded_logins,
            )
            per_pr[str(pr_number)].update(trace)
        else:
            result = suggest_reviewer_for_pr(
                pr_number=pr_number,
                pr_entry=pr_entry,
                reviewers=inputs.reviewers,
                assignment_stats=stats_copy,
                rng=rng,
                excluded_logins=excluded_logins,
            )

        if result.suggested is None:
            remaining_prs.remove(pr_number)
            round_index += 1
            continue

        suggestions[pr_number] = result.suggested
        open_list, weight, total = stats_copy.get(result.suggested, ([], 0.0, 0))
        open_list = list(open_list)
        open_list.append(pr_number)
        stats_copy[result.suggested] = (open_list, weight + 1, total + 1)

        remaining_prs.remove(pr_number)
        round_index += 1

    return SimulationResult(suggestions=suggestions, per_pr=per_pr, final_assignment_stats=stats_copy)
