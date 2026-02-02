from __future__ import annotations

from typing import Iterable

from django.db.models import QuerySet

from syncer.models import PullRequest, StatusContext


def latest_status_contexts_for_pr(
    pr: PullRequest,
    *,
    head_shas: Iterable[str] | None = None,
) -> "StatusContext.QuerySet":
    """Return latest StatusContext rows per (head_sha, name) for a PR.

    Note: This relies on Postgres DISTINCT ON semantics via distinct(fields).
    """
    qs: QuerySet[StatusContext] = StatusContext.objects.filter(pull_request=pr)
    if head_shas:
        qs = qs.filter(head_sha__in=list(head_shas))
    return qs.order_by("head_sha", "name", "-gh_created_at", "-id").distinct("head_sha", "name")
