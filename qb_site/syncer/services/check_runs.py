from __future__ import annotations

from typing import Iterable

from django.db.models import QuerySet
from django.db.models.functions import Coalesce

from syncer.models import CommitCheckRun, PullRequest


def latest_check_runs_for_pr(
    pr: PullRequest,
    *,
    head_shas: Iterable[str] | None = None,
) -> "CommitCheckRun.QuerySet":
    """Return latest commit-scoped CheckRun rows per (head_sha, name).

    Note: This relies on Postgres DISTINCT ON semantics via distinct(fields).
    """
    qs: QuerySet[CommitCheckRun] = CommitCheckRun.objects.filter(repository=pr.repository)
    if head_shas:
        qs = qs.filter(head_sha__in=list(head_shas))
    elif pr.head_sha:
        qs = qs.filter(head_sha=pr.head_sha)
    else:
        return qs.none()
    return qs.order_by(
        "head_sha",
        "name",
        Coalesce("gh_completed_at", "gh_started_at").desc(),
        "-id",
    ).distinct("head_sha", "name")
