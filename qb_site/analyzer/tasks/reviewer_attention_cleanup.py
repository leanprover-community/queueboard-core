from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from typing import Any

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from analyzer.models import (
    ReviewerAttentionAutoUnassignRecord,
    ReviewerAttentionDailyRun,
    ReviewerAttentionNotificationRecord,
)
from syncer.models import PullRequest, PullRequestState


def _normalize_login(login: str | None) -> str:
    return (login or "").strip().lower()


@shared_task(name="analyzer.reviewer_attention_cleanup")
def reviewer_attention_cleanup_task(
    *,
    notification_retention_days: int | None = None,
    auto_unassign_retention_days: int | None = None,
    run_retention_days: int | None = None,
) -> dict[str, Any]:
    """Prune stale reviewer-attention run-state rows.

    Safety rules for notification records:
    - delete only when older than retention and either:
      - PR is not currently open, or
      - reviewer is no longer assigned on that open PR.
    - keep rows for open PRs where reviewer is still assigned, because these rows
      carry once-per-cycle dedupe guarantees.
    """

    notif_days = int(
        notification_retention_days
        if notification_retention_days is not None
        else getattr(settings, "ANALYZER_REVIEWER_ATTENTION_NOTIFICATION_RETENTION_DAYS", 30)
    )
    unassign_days = int(
        auto_unassign_retention_days
        if auto_unassign_retention_days is not None
        else getattr(settings, "ANALYZER_REVIEWER_ATTENTION_AUTO_UNASSIGN_RETENTION_DAYS", 90)
    )
    run_days = int(
        run_retention_days
        if run_retention_days is not None
        else getattr(settings, "ANALYZER_REVIEWER_ATTENTION_RUN_RETENTION_DAYS", 30)
    )

    now = timezone.now()
    notification_cutoff = (now - timedelta(days=max(0, notif_days))).date()
    auto_unassign_cutoff = (now - timedelta(days=max(0, unassign_days))).date()
    run_cutoff_ts = now - timedelta(days=max(0, run_days))

    notification_candidates_qs = ReviewerAttentionNotificationRecord.objects.filter(
        run_date__lt=notification_cutoff
    ).select_related("reviewer")
    notification_candidates = list(
        notification_candidates_qs.only("id", "repository_id", "pr_number", "reviewer__github_login", "run_date")
    )
    notification_candidates_count = len(notification_candidates)

    pr_numbers_by_repo: dict[int, set[int]] = defaultdict(set)
    for record in notification_candidates:
        pr_numbers_by_repo[int(record.repository_id)].add(int(record.pr_number))

    open_assignees_by_repo_pr: dict[tuple[int, int], set[str]] = {}
    if pr_numbers_by_repo:
        repo_ids = list(pr_numbers_by_repo.keys())
        all_numbers = sorted({num for nums in pr_numbers_by_repo.values() for num in nums})
        open_pr_rows = PullRequest.objects.filter(
            repository_id__in=repo_ids,
            state=PullRequestState.OPEN,
            number__in=all_numbers,
        ).values_list("repository_id", "number", "assignees")
        for repo_id, pr_number, assignees in open_pr_rows:
            open_assignees_by_repo_pr[(int(repo_id), int(pr_number))] = {
                _normalize_login(str(login)) for login in (assignees or []) if _normalize_login(str(login))
            }

    deletable_notification_ids: list[int] = []
    for record in notification_candidates:
        repo_id = int(record.repository_id)
        pr_number = int(record.pr_number)
        reviewer_login = _normalize_login(getattr(record.reviewer, "github_login", None))
        current_assignees = open_assignees_by_repo_pr.get((repo_id, pr_number))
        if current_assignees is None:
            # PR is no longer open (or no longer exists in syncer rows).
            deletable_notification_ids.append(int(record.id))
            continue
        if reviewer_login not in current_assignees:
            # Reviewer is no longer assigned, so this dedupe record is no longer needed.
            deletable_notification_ids.append(int(record.id))

    notification_deleted = 0
    if deletable_notification_ids:
        deleted, _ = ReviewerAttentionNotificationRecord.objects.filter(id__in=deletable_notification_ids).delete()
        notification_deleted = int(deleted)

    auto_unassign_deleted, _ = ReviewerAttentionAutoUnassignRecord.objects.filter(run_date__lt=auto_unassign_cutoff).delete()
    runs_deleted, _ = ReviewerAttentionDailyRun.objects.filter(started_at__lt=run_cutoff_ts).delete()

    return {
        "notification_retention_days": notif_days,
        "auto_unassign_retention_days": unassign_days,
        "run_retention_days": run_days,
        "notification_cutoff": str(notification_cutoff.isoformat()),
        "auto_unassign_cutoff": str(auto_unassign_cutoff.isoformat()),
        "run_cutoff": run_cutoff_ts.isoformat(),
        "notifications": {
            "candidates": notification_candidates_count,
            "deleted": notification_deleted,
            "kept": max(notification_candidates_count - notification_deleted, 0),
        },
        "auto_unassign_records_deleted": int(auto_unassign_deleted),
        "runs_deleted": int(runs_deleted),
    }
