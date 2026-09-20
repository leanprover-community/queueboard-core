from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Sequence

from django.utils import timezone

from core.models import Repository
from queueboard.ci_status import CIStatus
from queueboard.classify_pr_state import determine_PR_status_ignoring_fork
from queueboard.util import transitive_dependency_counts


def _label_objects(labels: Iterable[object]) -> List[dict]:
    """Normalise a PR's labels to ``{name, color, url}`` dicts, dropping anything unusable.

    Snapshot payloads carry full label dicts, but older payloads (and hand-written test
    fixtures) may hold bare name strings; those get a null colour so the frontend can fall
    back to a neutral chip.
    """
    result: List[dict] = []
    for label in labels:
        if isinstance(label, dict):
            name = label.get("name")
            if not isinstance(name, str):
                continue
            result.append({"name": name, "color": label.get("color"), "url": label.get("url")})
        elif isinstance(label, str):
            result.append({"name": label, "color": None, "url": None})
    return result


def _is_draft(pr_entry: dict, label_names: Sequence[str]) -> bool:
    is_draft_flag = bool(pr_entry.get("is_draft"))
    lowered = {name.lower() for name in label_names}
    return is_draft_flag or any(name in {"wip", "draft"} for name in lowered)


def _int_key(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _dashboard_members(snapshot: dict, dashboard: str) -> set[int]:
    """PR numbers on one of the snapshot's dashboard lists.

    Queue membership is deliberately not ``pr_status == "AwaitingReview"``: it additionally
    applies the base-branch check, the rule set's required/forbidden labels and CI gating, so
    the two can legitimately disagree. Payloads without ``lists`` (older snapshots) yield an
    empty set rather than failing.
    """
    lists = snapshot.get("lists") or {}
    dashboards = lists.get("dashboards") or {}
    members: set[int] = set()
    for raw_number in dashboards.get(dashboard) or []:
        number = _int_key(raw_number)
        if number is not None:
            members.add(number)
    return members


def _status_ignoring_fork(entry: dict, label_names: Sequence[str], now, ci_gating_mode: str | None) -> str | None:
    """The PR's status with the fork question set aside; see determine_PR_status_ignoring_fork.

    ``pr_status`` is ``NotFromFork`` for every PR opened from a branch of the repository itself,
    which in mathlib is a large minority — and it is returned before labels or CI are examined,
    so on its own it cannot colour a graph. Recomputing here (rather than adding a field to the
    snapshot contract) keeps this presentational distinction out of the shared payload while
    still going through the one real classifier.
    """
    try:
        ci = CIStatus.from_string(entry.get("ci_status"))
    except KeyError:
        ci = CIStatus.Missing
    status = determine_PR_status_ignoring_fork(now, list(label_names), ci, bool(entry.get("is_draft")), ci_gating_mode)
    return str(status)


@dataclass
class DependencyGraphBuilder:
    """Build the legacy dependency_graph.json payload from a queueboard snapshot."""

    def build(self, *, repository: Repository, snapshot: dict) -> dict:
        prs_raw = snapshot.get("prs") or {}
        pr_numbers: List[int] = []
        pr_entries: Dict[int, dict] = {}

        for raw_number, pr_entry in prs_raw.items():
            number = _int_key(raw_number)
            if number is None:
                continue
            pr_numbers.append(number)
            pr_entries[number] = pr_entry or {}

        pr_numbers.sort()
        present = set(pr_numbers)
        on_queue = _dashboard_members(snapshot, "Queue")
        # "labelled maintainer-merge but not yet ready-to-merge", i.e. reviewed and waiting on a
        # maintainer. Such a PR stays on the review queue, but a reviewer can take no further
        # action on it (see QueueRuleSet.assignment_forbidden_label_names), so the graph must not
        # paint it the same as a PR that still needs reviewing.
        awaiting_maintainer_merge = _dashboard_members(snapshot, "AllMaintainerMerge")
        ci_gating_mode = (snapshot.get("meta") or {}).get("ci_gating_mode")
        now = timezone.now()

        dependencies: Dict[int, List[int]] = {}
        dependents: Dict[int, List[int]] = {num: [] for num in pr_numbers}
        labels_by_pr: Dict[int, List[dict]] = {}
        state_by_pr: Dict[int, str] = {}

        for pr_number in pr_numbers:
            entry = pr_entries[pr_number]
            labels_by_pr[pr_number] = _label_objects(entry.get("labels") or [])
            state_by_pr[pr_number] = str(entry.get("state", "")).lower()

            raw_deps = entry.get("direct_dependencies") or []
            filtered = []
            for dep in raw_deps:
                dep_num = _int_key(dep)
                if dep_num is None:
                    continue
                if dep_num in present:
                    filtered.append(dep_num)
            dependencies[pr_number] = filtered
            for dep in filtered:
                dependents[dep].append(pr_number)

        transitive = transitive_dependency_counts(dependencies)

        nodes: List[dict] = []
        links: List[dict] = []

        for pr_number in pr_numbers:
            entry = pr_entries[pr_number]
            labels = labels_by_pr[pr_number]
            counts = transitive[pr_number]
            label_names = [label["name"] for label in labels]
            nodes.append(
                {
                    "id": pr_number,
                    "title": entry.get("title"),
                    "author": entry.get("author"),
                    "state": state_by_pr[pr_number],
                    "is_draft": _is_draft(entry, label_names),
                    "labels": labels,
                    "url": f"https://github.com/{repository.owner}/{repository.name}/pull/{pr_number}",
                    "pr_status": entry.get("pr_status"),
                    "pr_status_ignoring_fork": _status_ignoring_fork(entry, label_names, now, ci_gating_mode),
                    "ci_status": entry.get("ci_status"),
                    "on_queue": pr_number in on_queue,
                    "awaiting_maintainer_merge": pr_number in awaiting_maintainer_merge,
                    "dependency_count": len(dependencies.get(pr_number, [])),
                    "dependent_count": len(dependents.get(pr_number, [])),
                    "upstream_count": counts.upstream,
                    "downstream_count": counts.downstream,
                    "additions": entry.get("additions"),
                    "deletions": entry.get("deletions"),
                }
            )

        for pr_number, deps in dependencies.items():
            for dep in deps:
                links.append(
                    {
                        "source": pr_number,
                        "target": dep,
                        "source_state": state_by_pr.get(pr_number, ""),
                        "target_state": state_by_pr.get(dep, ""),
                    }
                )

        metadata = {
            "total_prs": len(pr_numbers),
            "prs_with_dependencies": sum(1 for deps in dependencies.values() if deps),
            "prs_that_are_dependencies": sum(1 for deps in dependents.values() if deps),
            "dependency_links": len(links),
            "prs_on_queue": sum(1 for num in pr_numbers if num in on_queue),
        }

        return {"nodes": nodes, "links": links, "metadata": metadata}
