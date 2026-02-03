from __future__ import annotations

from typing import Iterable

from django.db.models import QuerySet
from django.db.models.functions import Coalesce

from syncer.models import CheckRun, PullRequest


def latest_check_runs_for_pr(
    pr: PullRequest,
    *,
    head_shas: Iterable[str] | None = None,
) -> "CheckRun.QuerySet":
    """Return latest CheckRun rows per (head_sha, name) for a PR.

    Note: This relies on Postgres DISTINCT ON semantics via distinct(fields).
    """
    qs: QuerySet[CheckRun] = CheckRun.objects.filter(pull_request=pr)
    if head_shas:
        qs = qs.filter(head_sha__in=list(head_shas))
    return qs.order_by(
        "head_sha",
        "name",
        Coalesce("gh_completed_at", "gh_started_at").desc(),
        "-id",
    ).distinct("head_sha", "name")
