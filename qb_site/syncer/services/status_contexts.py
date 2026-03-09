from __future__ import annotations

from typing import Iterable

from django.db.models import QuerySet

from syncer.models import CommitStatusContext, PullRequest


def latest_status_contexts_for_pr(
    pr: PullRequest,
    *,
    head_shas: Iterable[str] | None = None,
) -> "CommitStatusContext.QuerySet":
    """Return latest commit-scoped StatusContext rows per (head_sha, name).

    Note: This relies on Postgres DISTINCT ON semantics via distinct(fields).
    """
    qs: QuerySet[CommitStatusContext] = CommitStatusContext.objects.filter(repository=pr.repository)
    if head_shas:
        qs = qs.filter(head_sha__in=list(head_shas))
    elif pr.head_sha:
        qs = qs.filter(head_sha=pr.head_sha)
    else:
        return qs.none()
    return qs.order_by("head_sha", "name", "-gh_created_at", "-id").distinct("head_sha", "name")
