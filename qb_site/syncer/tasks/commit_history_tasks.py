from __future__ import annotations

from celery import shared_task

from syncer.models import PullRequest, CheckRun, StatusContext
from syncer.services.github_client import GitHubClient
from syncer.services.commit_history import harvest_commit_history_with_cursor
from syncer.tasks.sync_tasks import sync_ci_for_shas_task


@shared_task(name="syncer.harvest_commit_history")
def harvest_commit_history_task(
    pr_id: int, start_sha: str, max_pages: int = 1, page_size: int = 20, since_iso: str | None = None
) -> dict:
    """Celery task to harvest commit history for a PR/start_sha; returns harvested shas and cursor state."""
    pr = PullRequest.objects.select_related("repository").filter(id=int(pr_id)).first()
    if pr is None:
        return {"skipped": True, "reason": "pr_not_found"}

    client = GitHubClient()
    shas, state = harvest_commit_history_with_cursor(
        client=client,
        pr=pr,
        start_sha=start_sha,
        max_pages=max_pages,
        page_size=page_size,
        since_iso=since_iso,
    )
    missing = []
    for sha in shas:
        has_cr = CheckRun.objects.filter(pull_request=pr, head_sha=sha).exists()
        has_sc = StatusContext.objects.filter(pull_request=pr, head_sha=sha).exists()
        if not (has_cr or has_sc):
            missing.append(sha)
    ci_task_id = None
    if missing:
        ci_res = sync_ci_for_shas_task.delay(
            repo_id=pr.repository_id,
            number=pr.number,
            shas=missing,
            max_pages_per_sha=1,
            require_pr_association=False,
        )
        ci_task_id = ci_res.id
    return {
        "skipped": False,
        "pr_id": pr.id,
        "start_sha": start_sha,
        "harvested_shas": shas,
        "has_more": state.has_more,
        "cursor": state.cursor,
        "attempts": state.attempts,
        "ci_task_id": ci_task_id,
        "ci_missing": missing,
    }


@shared_task(name="syncer.harvest_commit_history_sweep")
def harvest_commit_history_sweep(max_jobs: int = 25, max_pages: int = 1, page_size: int = 20) -> dict:
    """Sweep CommitHistoryHarvest rows with has_more=True and enqueue harvest tasks."""
    jobs = 0
    enqueued: list[dict] = []
    qs = (
        PullRequest.objects.filter(commit_history_harvests__has_more=True)
        .select_related("repository")
        .prefetch_related("commit_history_harvests")
        .distinct()
    )
    for pr in qs:
        for ch in pr.commit_history_harvests.filter(has_more=True).order_by("updated_at"):
            if jobs >= max_jobs:
                return {"enqueued": enqueued, "truncated": True}
            async_res = harvest_commit_history_task.delay(
                pr_id=pr.id,
                start_sha=ch.start_sha,
                max_pages=max_pages,
                page_size=page_size,
                since_iso=ch.cutoff_ts.isoformat() if ch.cutoff_ts else None,
            )
            enqueued.append({"pr_id": pr.id, "start_sha": ch.start_sha, "task_id": async_res.id})
            jobs += 1
    return {"enqueued": enqueued, "truncated": False}
