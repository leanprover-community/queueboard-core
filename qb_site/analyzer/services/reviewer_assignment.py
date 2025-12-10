from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, Sequence

from django.utils.dateparse import parse_datetime

from analyzer.models import AreaStatsSnapshot, QueueRuleSet, QueueSnapshot, ReviewerAssignmentSnapshot
from analyzer.services.queueboard_snapshot import QueueboardSnapshotBuilder
from core.models import Repository, ReviewerPreference
from queueboard.classify_pr_state import PRStatus

DataStatus = str  # "valid" | "incomplete" | "missing"


def _isoformat(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _normalize_login(login: str | None) -> str:
    return (login or "").strip().lower()


def _is_topic_label(name: str | None) -> bool:
    if not name:
        return False
    lowered = name.lower()
    return lowered.startswith("t-") or lowered in {"ci", "imo", "tech debt"}


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
class AssignmentStatistics:
    timestamp: datetime
    num_open: int
    assigned_open: list[int]
    number_multiple_assignees: int
    assignments: Dict[str, tuple[list[int], float, int]]


@dataclass
class ReviewerSuggestionResult:
    suggested: str | None
    all_potential_reviewers: list[str]
    all_available_reviewers: list[str]
    reason: str | None = None


def build_reviewer_catalog(repository: Repository, *, now: datetime | None = None) -> list[ReviewerProfile]:
    """Hydrate reviewer profiles from ReviewerPreference rows."""
    current_time = now or datetime.now(timezone.utc)
    profiles: list[ReviewerProfile] = []
    prefs = ReviewerPreference.objects.filter(repository=repository).select_related("user").order_by("user__github_login")
    for pref in prefs:
        login = getattr(pref.user, "github_login", None)
        if not login:
            continue
        temporary_break = bool(pref.away_until and pref.away_until > current_time)
        preferred_labels = list(pref.preferred_labels or [])
        conflicts = list(pref.conflict_of_interest or [])
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


def _topic_labels(pr_entry: dict) -> list[str]:
    labels = pr_entry.get("labels") or []
    names: list[str] = []
    for label in labels:
        name = label.get("name")
        if _is_topic_label(name):
            names.append(name)
    return names


def _current_weight(login: str, assignments: Dict[str, tuple[list[int], float, int]]) -> float:
    data = assignments.get(login)
    return float(data[1]) if data else 0.0


def suggest_reviewer_for_pr(
    *,
    pr_number: int,
    pr_entry: dict,
    reviewers: Sequence[ReviewerProfile],
    assignment_stats: Dict[str, tuple[list[int], float, int]],
    rng: random.Random | None = None,
) -> ReviewerSuggestionResult:
    labels = _topic_labels(pr_entry)
    labels_lower = [lab.lower() for lab in labels]
    author = pr_entry.get("author") or ""
    author_norm = _normalize_login(author)

    matching: list[tuple[ReviewerProfile, list[str]]] = []
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
        return ReviewerSuggestionResult(suggested=None, all_potential_reviewers=[], all_available_reviewers=[], reason="no-match")

    if not labels_lower:
        proposed = matching
    else:
        max_score = max(len(m) for _, m in matching)
        if max_score > 1:
            proposed = [(rev, m) for rev, m in matching if len(m) == max_score]
        else:
            proposed = [(rev, m) for rev, m in matching if m]
        if not proposed:
            return ReviewerSuggestionResult(
                suggested=None,
                all_potential_reviewers=[],
                all_available_reviewers=[],
                reason="no-matching-labels",
            )

    proposed_sorted = sorted(proposed, key=lambda item: _current_weight(item[0].github_login, assignment_stats))
    all_potential = [rev.github_login for rev, _ in proposed_sorted]

    available: list[str] = []
    available_weights: list[float] = []
    for reviewer, _ in proposed_sorted:
        current_weight = _current_weight(reviewer.github_login, assignment_stats)
        remaining = reviewer.maximum_capacity - current_weight
        if remaining > 0 and reviewer.auto_assign and not reviewer.temporary_break:
            available.append(reviewer.github_login)
            available_weights.append(remaining)

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


def suggest_reviewers_many(
    *,
    reviewers: Sequence[ReviewerProfile],
    assignments: Dict[str, tuple[list[int], float, int]],
    prs_to_assign: Iterable[int],
    all_prs: Dict[int | str, dict],
    rng: random.Random | None = None,
) -> dict[int, str]:
    """Suggest reviewers for many PRs, updating in-memory load after each pick."""
    stats_copy: Dict[str, tuple[list[int], float, int]] = {
        login: (list(open_list), float(weight), int(total)) for login, (open_list, weight, total) in assignments.items()
    }
    suggestions: dict[int, str] = {}

    for pr_number in prs_to_assign:
        pr_entry = all_prs.get(pr_number) or all_prs.get(str(pr_number))
        if not pr_entry:
            continue
        result = suggest_reviewer_for_pr(
            pr_number=pr_number,
            pr_entry=pr_entry,
            reviewers=reviewers,
            assignment_stats=stats_copy,
            rng=rng,
        )
        if result.suggested is None:
            continue
        suggestions[pr_number] = result.suggested
        open_list, weight, total = stats_copy.get(result.suggested, ([], 0.0, 0))
        open_list = list(open_list)
        open_list.append(pr_number)
        stats_copy[result.suggested] = (open_list, weight + 1, total + 1)

    return suggestions


def compute_area_stats(
    *,
    existing_assignments: Dict[str, tuple[list[int], float, int]],
    reviewers: Sequence[ReviewerProfile],
    queue_pr_numbers: Sequence[int],
    all_prs: Dict[int | str, dict],
    rng: random.Random | None = None,
) -> dict[str, dict]:
    """Compute area-level metrics for queued PRs."""
    area_data: dict[str, dict] = {}
    reviewer_logins_lower = {_normalize_login(r.github_login) for r in reviewers}

    for pr_number in queue_pr_numbers:
        pr_entry = all_prs.get(pr_number) or all_prs.get(str(pr_number))
        if not pr_entry:
            continue
        topic_labels = [lab for lab in pr_entry.get("labels") or [] if _is_topic_label(lab.get("name"))]
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

        reviewers = build_reviewer_catalog(repository, now=current_time)
        assignment_stats = collect_assignment_statistics(payload)

        dashboards = payload.get("lists", {}).get("dashboards", {})
        queue_prs = dashboards.get("Queue", [])
        stale_unassigned = dashboards.get("QueueStaleUnassigned", [])

        automatic_assignments = suggest_reviewers_many(
            reviewers=reviewers,
            assignments=assignment_stats.assignments,
            prs_to_assign=stale_unassigned,
            all_prs=payload.get("prs", {}),
            rng=self.rng,
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
        dashboards = payload.get("lists", {}).get("dashboards", {})
        queue_prs = dashboards.get("Queue", [])

        area_stats = compute_area_stats(
            existing_assignments=assignment_stats.assignments,
            reviewers=reviewers,
            queue_pr_numbers=queue_prs,
            all_prs=payload.get("prs", {}),
            rng=self.rng,
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
