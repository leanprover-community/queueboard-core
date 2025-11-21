from __future__ import annotations

from django.db import models


class SyncerMetricsSnapshot(models.Model):
    """Aggregated syncer metrics for a fixed time window.

    Collected periodically (default: every 15 minutes) by a Celery beat task.
    Each row summarizes activity and resource usage observed during
    ``[window_start, window_start + window_seconds)``.

    Fields intentionally focus on a small set of decision-making metrics; we can
    extend this model as we learn what’s most useful.
    """

    window_start = models.DateTimeField(db_index=True)
    window_seconds = models.PositiveIntegerField(default=900)

    # PR task throughput
    pr_tasks = models.IntegerField(default=0)
    pr_deferred = models.IntegerField(default=0)
    pr_failures = models.IntegerField(default=0)
    pr_avg_duration_s = models.FloatField(default=0.0)
    pr_token_cost = models.IntegerField(default=0)  # sum of rate_events.cost when present

    # Repo task throughput
    repo_tasks = models.IntegerField(default=0)
    repo_low_budget = models.IntegerField(default=0)
    repo_avg_duration_s = models.FloatField(default=0.0)
    repo_discovered = models.IntegerField(default=0)
    repo_enqueued = models.IntegerField(default=0)
    repo_discovery_cost = models.IntegerField(default=0)

    # DB activity
    rows_pull_request = models.IntegerField(default=0)
    rows_timeline_event = models.IntegerField(default=0)
    rows_check_run = models.IntegerField(default=0)
    rows_status_context = models.IntegerField(default=0)
    rows_pr_label = models.IntegerField(default=0)
    rows_label_def = models.IntegerField(default=0)

    # Database size at snapshot time (bytes)
    db_size_bytes = models.BigIntegerField(default=0)

    # Broker queue depth at snapshot time (Redis LLEN; null when unavailable)
    queue_default_depth = models.IntegerField(null=True, blank=True, default=None)
    queue_github_depth = models.IntegerField(null=True, blank=True, default=None)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-window_start"]

    def __str__(self) -> str:  # pragma: no cover - simple display
        return f"Metrics {self.window_start.isoformat()} +{self.window_seconds}s"
