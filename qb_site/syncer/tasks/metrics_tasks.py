from __future__ import annotations

"""Periodic metrics collector for the Syncer.

Collects a compact set of metrics every N seconds (default: 900s = 15 minutes) and
persists them to ``SyncerMetricsSnapshot``. The collector summarizes:
- PR/Repo task throughput and durations from django-celery-results
- Low-budget/deferred counts
- Discovery/enqueue totals
- Optional token cost totals (from instrumented per-PR ``rate_events`` and repo discovery cost)
- DB row inserts in the window and total database size

This snapshot enables sizing hosting resources and monitoring token usage trends
without parsing logs in production.
"""

from datetime import timedelta
from typing import Any, Dict

from celery import shared_task
from django.utils import timezone
from django.db import connection

from django_celery_results.models import TaskResult

from syncer.models import (
    SyncerMetricsSnapshot,
    PullRequest,
    PRTimelineEvent,
    CheckRun,
    StatusContext,
    PRLabel,
    LabelDef,
)


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
    for tr in pr_q.only("result"):
        res = _parse_json(tr.result)
        events = res.get("rate_events") or []
        if isinstance(events, list):
            for ev in events:
                try:
                    pr_cost += int(ev.get("cost") or 0)
                except Exception:
                    pass

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
    for tr in repo_q.only("result"):
        res = _parse_json(tr.result)
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

    # DB activity (rows created in the window)
    rows_pr = PullRequest.objects.filter(created_at__gte=start, created_at__lt=now).count()
    rows_tl = PRTimelineEvent.objects.filter(created_at__gte=start, created_at__lt=now).count()
    rows_cr = CheckRun.objects.filter(created_at__gte=start, created_at__lt=now).count()
    rows_sc = StatusContext.objects.filter(created_at__gte=start, created_at__lt=now).count()
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
        rows_pull_request=rows_pr,
        rows_timeline_event=rows_tl,
        rows_check_run=rows_cr,
        rows_status_context=rows_sc,
        rows_pr_label=rows_pl,
        rows_label_def=rows_ld,
        db_size_bytes=db_size,
    )

    return {
        "id": snap.id,
        "window_start": snap.window_start.isoformat(),
        "pr_tasks": pr_count,
        "repo_tasks": repo_count,
        "db_size_bytes": db_size,
    }
