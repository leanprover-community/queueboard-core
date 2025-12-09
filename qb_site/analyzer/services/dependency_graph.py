from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Sequence

from core.models import Repository


def _label_names(labels: Iterable[object]) -> List[str]:
    names: List[str] = []
    for label in labels:
        if isinstance(label, dict):
            name = label.get("name")
            if isinstance(name, str):
                names.append(name)
        elif isinstance(label, str):
            names.append(label)
    return names


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

        dependencies: Dict[int, List[int]] = {}
        dependents: Dict[int, List[int]] = {num: [] for num in pr_numbers}
        label_names_by_pr: Dict[int, List[str]] = {}
        state_by_pr: Dict[int, str] = {}

        for pr_number in pr_numbers:
            entry = pr_entries[pr_number]
            labels = entry.get("labels") or []
            names = _label_names(labels)
            label_names_by_pr[pr_number] = names
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

        nodes: List[dict] = []
        links: List[dict] = []

        for pr_number in pr_numbers:
            entry = pr_entries[pr_number]
            names = label_names_by_pr[pr_number]
            nodes.append(
                {
                    "id": pr_number,
                    "title": entry.get("title"),
                    "author": entry.get("author"),
                    "state": state_by_pr[pr_number],
                    "is_draft": _is_draft(entry, names),
                    "labels": names,
                    "url": f"https://github.com/{repository.owner}/{repository.name}/pull/{pr_number}",
                    "dependency_count": len(dependencies.get(pr_number, [])),
                    "dependent_count": len(dependents.get(pr_number, [])),
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
        }

        return {"nodes": nodes, "links": links, "metadata": metadata}
