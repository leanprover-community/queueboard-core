from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

from core.models import Repository
from syncer.models import PullRequest
from syncer.services.ci_backoff import should_enqueue_ci_sha
from analyzer.services.revisions import next_revision_backfill_shas


@dataclass
class PlanItem:
    pr: PullRequest
    shas: List[str]


def plan_missing_ci_shas(
    *,
    repo: Repository,
    pr_numbers: Optional[Sequence[int]] = None,
    limit_per_pr: int = 2,
) -> List[PlanItem]:
    """Plan CI-by-SHA backfill using revision windows.

    - Selects PRs in ``repo`` (optionally restricted by ``pr_numbers``).
    - For each PR, returns up to ``limit_per_pr`` head SHAs from PRRevision
      that appear to have no CI snapshots in syncer tables.
    - Skips PRs with no missing heads.

    This is a pure planner; it does not enqueue any tasks.
    """
    qs = PullRequest.objects.filter(repository=repo).only("id", "number", "repository_id")
    if pr_numbers:
        qs = qs.filter(number__in=list(pr_numbers))
    out: List[PlanItem] = []
    for pr in qs:
        shas = next_revision_backfill_shas(pr, limit=int(limit_per_pr))
        if shas:
            out.append(PlanItem(pr=pr, shas=list(shas)))
    return out


def enqueue_ci_by_shas(
    *,
    pr: PullRequest,
    shas: Sequence[str],
    pages_per_sha: int,
    require_pr_association: bool = False,
) -> str:
    """Enqueue a syncer task to fetch CI for specific commit SHAs for ``pr``.

    - Returns the Celery task id.
    - Uses Syncer's rate-aware scheduling and continuation.
    - ``require_pr_association`` is conservative; leave False when backfilling historical
      heads after force-pushes (association may no longer be reported by GitHub).
    """
    # Local import to keep Analyzer decoupled at import time and easy to test
    from syncer.tasks.sync_tasks import sync_ci_for_shas_task  # type: ignore

    filtered = [sha for sha in shas if sha and should_enqueue_ci_sha(pr=pr, sha=sha, reason="analyzer.enqueue_ci_by_shas")]
    if not filtered:
        return ""
    res = sync_ci_for_shas_task.delay(
        repo_id=pr.repository_id,
        number=int(pr.number),
        shas=list(filtered),
        max_pages_per_sha=int(pages_per_sha),
        require_pr_association=bool(require_pr_association),
    )
    return str(res.id)
