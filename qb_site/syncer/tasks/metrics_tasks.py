"""Periodic metrics collector for the Syncer.

Collects a compact set of metrics every N seconds (default: 900s = 15 minutes) and
persists them to ``SyncerMetricsSnapshot``. The collector summarizes:
- PR/Repo task throughput and durations from django-celery-results
- Low-budget/deferred counts
- Discovery/enqueue totals
- Token cost totals (from instrumented per-PR ``rate_events``, repo discovery cost, and any other task snapshots)
- DB row inserts in the window and total database size

This snapshot enables sizing hosting resources and monitoring token usage trends
without parsing logs in production.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict

from celery import shared_task
from django.conf import settings
from django.utils import timezone
from django.db import connection

from django_celery_results.models import TaskResult

from syncer.models import (
    SyncerMetricsSnapshot,
    PullRequest,
    PRTimelineEvent,
    CommitCheckRun,
    CommitStatusContext,
    PRLabel,
    LabelDef,
    GitHubWebhookDelivery,
)
from syncer.services import rate_budget as rb


def _parse_json(raw: Any) -> Dict[str, Any]:
    import json

    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _queue_depth(queue_name: str) -> int | None:
    """Return LLEN for a Redis queue or None if unavailable."""
    if not queue_name:
        return None
    client = rb._get_redis_client()
    if client is None:
        return None
    try:
        return int(client.llen(queue_name))
    except Exception:
        return None


def _token_cost_from_result(res: Dict[str, Any]) -> int:
    """Extract token cost from a task result without double-counting."""

    if not isinstance(res, dict):
        return 0

    def _as_int(val: Any) -> int:
        try:
            return int(val)
        except Exception:
            return 0

    total = 0
    events = res.get("rate_events")
    has_events = isinstance(events, list)
    if has_events and events:
        for ev in events:
            if isinstance(ev, dict):
                total += _as_int(ev.get("cost"))
    if "discovery_cost" in res:
        total += _as_int(res.get("discovery_cost"))
    elif not has_events or not events:
        rl = res.get("rate_limit") or {}
        if isinstance(rl, dict):
            total += _as_int(rl.get("cost"))
    return total


@shared_task(name="syncer.collect_metrics")
def collect_metrics_task() -> Dict[str, Any]:  # type: ignore[no-redef]
    """Collect and persist a metrics snapshot for the last 15 minutes.

    Window: ``[now - 900s, now)``.
    """
    now = timezone.now()
    window_seconds = 900
    start = now - timedelta(seconds=window_seconds)

    # Query task results in the window
    q = TaskResult.objects.filter(date_done__gte=start, date_done__lt=now)
    repo_q = q.filter(task_name="syncer.sync_repo_since")
    pr_q = q.filter(task_name="syncer.sync_pr")

    def _iter_results(qs):
        for tr in qs.only("result"):
            yield _parse_json(tr.result)

    # PR tasks
    pr_count = pr_q.count()
    pr_deferred = pr_q.filter(result__contains='"reason": "deferred_low_budget"').count()
    pr_fail = pr_q.filter(status="FAILURE").count()
    if pr_count:
        from django.db.models import F, ExpressionWrapper, DurationField, Avg

        dur = ExpressionWrapper(F("date_done") - F("date_created"), output_field=DurationField())
        avg_dur = pr_q.annotate(_d=dur).aggregate(Avg("_d"))["_d__avg"]
        pr_avg_s = avg_dur.total_seconds() if avg_dur else 0.0
    else:
        pr_avg_s = 0.0
    # Sum token cost from rate_events if present
    pr_cost = 0
    pr_cost_total = 0
    for res in _iter_results(pr_q):
        events = res.get("rate_events") or []
        if isinstance(events, list):
            for ev in events:
                try:
                    pr_cost += int(ev.get("cost") or 0)
                except Exception:
                    pass
        pr_cost_total += _token_cost_from_result(res)

    # Repo tasks
    repo_count = repo_q.count()
    repo_low_budget = repo_q.filter(result__contains='"low_budget": true').count()
    if repo_count:
        from django.db.models import F, ExpressionWrapper, DurationField, Avg

        dur = ExpressionWrapper(F("date_done") - F("date_created"), output_field=DurationField())
        avg_dur = repo_q.annotate(_d=dur).aggregate(Avg("_d"))["_d__avg"]
        repo_avg_s = avg_dur.total_seconds() if avg_dur else 0.0
    else:
        repo_avg_s = 0.0

    repo_discovered = 0
    repo_enqueued = 0
    repo_disc_cost = 0
    repo_cost_total = 0
    for res in _iter_results(repo_q):
        try:
            repo_discovered += int(res.get("discovered") or 0)
            repo_enqueued += int(res.get("enqueued") or 0)
            # Prefer explicit discovery_cost if we add it later; fall back to rate_limit.cost
            if "discovery_cost" in res:
                repo_disc_cost += int(res.get("discovery_cost") or 0)
            else:
                rl = res.get("rate_limit") or {}
                repo_disc_cost += int(rl.get("cost") or 0)
        except Exception:
            pass
        repo_cost_total += _token_cost_from_result(res)

    # Other tasks (backfill/commit-history/CI) token cost
    other_cost_total = 0
    other_q = q.exclude(task_name__in=["syncer.sync_pr", "syncer.sync_repo_since"])
    for res in _iter_results(other_q):
        other_cost_total += _token_cost_from_result(res)

    token_cost_total = pr_cost_total + repo_cost_total + other_cost_total

    # Webhook delivery activity in the window
    webhook_q = GitHubWebhookDelivery.objects.filter(received_at__gte=start, received_at__lt=now)
    webhook_deliveries = webhook_q.count()
    webhook_route_pull_request = webhook_q.filter(summary_json__contains={"route": "pull_request"}).count()
    webhook_route_check = webhook_q.filter(summary_json__contains={"route": "check"}).count()
    webhook_route_noop = webhook_q.filter(summary_json__contains={"route": "noop"}).count()
    webhook_check_deliveries = webhook_route_check
    webhook_sha_first_tasks_enqueued = 0
    webhook_reason_enqueued_sync_pr = webhook_q.filter(summary_json__contains={"reason": "enqueued_sync_pr"}).count()
    webhook_reason_enqueued_sync_ci = webhook_q.filter(summary_json__contains={"reason": "enqueued_sync_ci"}).count()
    webhook_reason_deduped_sync_pr = webhook_q.filter(summary_json__contains={"reason": "deduped_sync_pr"}).count()
    webhook_reason_deduped_sync_ci = webhook_q.filter(summary_json__contains={"reason": "deduped_sync_ci"}).count()
    webhook_reason_ignored_action = webhook_q.filter(summary_json__contains={"reason": "ignored_action"}).count()
    webhook_deduped_sync_pr_total = 0
    webhook_deduped_sync_ci_total = 0
    for delivery in webhook_q.only("summary_json"):
        summary = delivery.summary_json if isinstance(delivery.summary_json, dict) else {}
        if (
            summary.get("route") == "check"
            and summary.get("check_sync_mode") == "sha_first"
            and summary.get("reason") == "enqueued_sync_ci"
        ):
            try:
                webhook_sha_first_tasks_enqueued += int(summary.get("enqueued_sync_ci") or 0)
            except Exception:
                pass
        try:
            webhook_deduped_sync_pr_total += int(summary.get("deduped_sync_prs") or 0)
        except Exception:
            pass
        try:
            webhook_deduped_sync_ci_total += int(summary.get("deduped_sync_ci") or 0)
        except Exception:
            pass
    webhook_duplicates_touched = webhook_q.filter(last_duplicate_at__gte=start, last_duplicate_at__lt=now).count()
    sha_task_impacted_pr_fanout_total = 0
    for res in _iter_results(q.filter(task_name="syncer.sync_ci_for_repo_shas")):
        try:
            sha_task_impacted_pr_fanout_total += int(res.get("impacted_pr_count") or 0)
        except Exception:
            pass

    # DB activity (rows created in the window)
    rows_pr = PullRequest.objects.filter(created_at__gte=start, created_at__lt=now).count()
    rows_tl = PRTimelineEvent.objects.filter(created_at__gte=start, created_at__lt=now).count()
    rows_cr = CommitCheckRun.objects.filter(created_at__gte=start, created_at__lt=now).count()
    rows_sc = CommitStatusContext.objects.filter(created_at__gte=start, created_at__lt=now).count()
    rows_pl = PRLabel.objects.filter(created_at__gte=start, created_at__lt=now).count()
    rows_ld = LabelDef.objects.filter(created_at__gte=start, created_at__lt=now).count()

    # DB size at snapshot
    db_size = 0
    try:
        with connection.cursor() as cur:
            cur.execute("select pg_database_size(current_database())")
            row = cur.fetchone()
            if row:
                db_size = int(row[0])
    except Exception:
        db_size = 0

    # Queue depths (may be None when Redis or broker key is unavailable)
    queue_default_depth = _queue_depth(getattr(settings, "CELERY_TASK_DEFAULT_QUEUE", "celery"))
    queue_github_depth = _queue_depth(getattr(settings, "SYNCER_GITHUB_QUEUE", ""))

    snap = SyncerMetricsSnapshot.objects.create(
        window_start=start,
        window_seconds=window_seconds,
        pr_tasks=pr_count,
        pr_deferred=pr_deferred,
        pr_failures=pr_fail,
        pr_avg_duration_s=pr_avg_s,
        pr_token_cost=pr_cost,
        repo_tasks=repo_count,
        repo_low_budget=repo_low_budget,
        repo_avg_duration_s=repo_avg_s,
        repo_discovered=repo_discovered,
        repo_enqueued=repo_enqueued,
        repo_discovery_cost=repo_disc_cost,
        webhook_deliveries=webhook_deliveries,
        webhook_route_pull_request=webhook_route_pull_request,
        webhook_route_check=webhook_route_check,
        webhook_route_noop=webhook_route_noop,
        webhook_check_deliveries=webhook_check_deliveries,
        webhook_sha_first_tasks_enqueued=webhook_sha_first_tasks_enqueued,
        webhook_reason_enqueued_sync_pr=webhook_reason_enqueued_sync_pr,
        webhook_reason_enqueued_sync_ci=webhook_reason_enqueued_sync_ci,
        webhook_reason_deduped_sync_pr=webhook_reason_deduped_sync_pr,
        webhook_reason_deduped_sync_ci=webhook_reason_deduped_sync_ci,
        webhook_reason_ignored_action=webhook_reason_ignored_action,
        webhook_deduped_sync_pr_total=webhook_deduped_sync_pr_total,
        webhook_deduped_sync_ci_total=webhook_deduped_sync_ci_total,
        webhook_duplicates_touched=webhook_duplicates_touched,
        sha_task_impacted_pr_fanout_total=sha_task_impacted_pr_fanout_total,
        token_cost_total=token_cost_total,
        rows_pull_request=rows_pr,
        rows_timeline_event=rows_tl,
        rows_check_run=rows_cr,
        rows_status_context=rows_sc,
        rows_pr_label=rows_pl,
        rows_label_def=rows_ld,
        db_size_bytes=db_size,
        queue_default_depth=queue_default_depth,
        queue_github_depth=queue_github_depth,
    )

    return {
        "id": snap.id,
        "window_start": snap.window_start.isoformat(),
        "pr_tasks": pr_count,
        "repo_tasks": repo_count,
        "webhook_deliveries": webhook_deliveries,
        "webhook_check_deliveries": webhook_check_deliveries,
        "webhook_sha_first_tasks_enqueued": webhook_sha_first_tasks_enqueued,
        "sha_task_impacted_pr_fanout_total": sha_task_impacted_pr_fanout_total,
        "db_size_bytes": db_size,
        "queue_default_depth": queue_default_depth,
        "queue_github_depth": queue_github_depth,
        "token_cost_total": token_cost_total,
    }
