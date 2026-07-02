from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha1
from typing import List, Set

from analyzer.models import PRDependency
from syncer.models import PullRequest

DEPENDENCY_PATTERN = re.compile(r"-\s*\[[ x]\]\s*depends on:\s*#(\d+)", re.IGNORECASE)
PR_DEPENDENCY_BUILDER_VERSION = 1


def parse_dependency_numbers(body: str | None) -> List[int]:
    """Extract dependent PR numbers from a PR body."""
    if not body:
        return []
    matches = DEPENDENCY_PATTERN.findall(body)
    numbers = {int(val) for val in matches}
    return sorted(numbers)


def body_hash(body: str | None) -> str:
    """Return a stable hash of the PR body for change detection."""
    data = (body or "").encode("utf-8")
    return sha1(data).hexdigest()


@dataclass
class DependencyRebuildResult:
    created: int
    updated: int
    deleted: int
    parsed_numbers: List[int]
    resolved_numbers: List[int]
    unresolved_numbers: List[int]


def rebuild_pr_dependencies(pr: PullRequest) -> DependencyRebuildResult:
    """Rebuild PRDependency edges for ``pr`` based on its body content."""
    parsed_numbers: Set[int] = set(parse_dependency_numbers(getattr(pr, "body", "")))
    # Ignore self-references if present.
    parsed_numbers.discard(int(pr.number))

    # Preload targets that already exist in the same repository.
    targets = {
        pr_obj.number: pr_obj
        for pr_obj in PullRequest.objects.filter(repository=pr.repository, number__in=list(parsed_numbers)).only(
            "id", "number", "repository_id"
        )
    }

    existing = {
        (dep.depends_on_repository_id, dep.depends_on_number): dep for dep in PRDependency.objects.filter(pull_request=pr)
    }

    created = 0
    updated = 0
    resolved: Set[int] = set()
    unresolved: Set[int] = set()

    for number in parsed_numbers:
        target_pr = targets.get(number)
        key = (pr.repository_id, number)
        dep = existing.get(key)
        created_now = False
        if dep is None:
            # The per-PR dependency task and the dependencies sweep can rebuild the
            # same PR concurrently; get_or_create absorbs losing the insert race on
            # the (pull_request, depends_on_repository, depends_on_number) key and
            # falls through to the update path with the winner's row.
            dep, created_now = PRDependency.objects.get_or_create(
                pull_request=pr,
                depends_on_repository=pr.repository,
                depends_on_number=number,
                defaults={"depends_on_pull_request": target_pr},
            )
            if created_now:
                created += 1
        if not created_now:
            desired_target_id = target_pr.id if target_pr else None
            if dep.depends_on_pull_request_id != desired_target_id:
                dep.depends_on_pull_request = target_pr
                dep.save(update_fields=["depends_on_pull_request", "updated_at"])
                updated += 1

        if target_pr is not None:
            resolved.add(number)
        else:
            unresolved.add(number)

    qs = PRDependency.objects.filter(pull_request=pr)
    if parsed_numbers:
        qs = qs.exclude(depends_on_number__in=parsed_numbers, depends_on_repository=pr.repository)
    deleted, _ = qs.delete()

    return DependencyRebuildResult(
        created=created,
        updated=updated,
        deleted=deleted,
        parsed_numbers=sorted(parsed_numbers),
        resolved_numbers=sorted(resolved),
        unresolved_numbers=sorted(unresolved),
    )
