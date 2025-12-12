from __future__ import annotations

import json
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from django_celery_results.models import TaskResult

from syncer.tasks.metrics_tasks import collect_metrics_task
from syncer.models import SyncerMetricsSnapshot


class TestCollectMetrics(TestCase):
    def _mk_task(self, name: str, result: dict, status: str = "SUCCESS", created_delta_s: int = 50, done_delta_s: int = 10):
        now = timezone.now()
        tr = TaskResult.objects.create(
            task_id=f"t-{name}-{now.timestamp()}",
            task_name=name,
            status=status,
            result=json.dumps(result),
            date_created=now - timedelta(seconds=created_delta_s),
            date_done=now - timedelta(seconds=done_delta_s),
        )
        return tr

    def test_collect_writes_snapshot(self) -> None:
        # Seed a PR task with rate_events and a repo task with discovery stats
        self._mk_task(
            "syncer.sync_pr",
            {
                "repo": "o/r",
                "number": 1,
                "rate_events": [{"label": "pr_bundle", "cost": 50}],
            },
        )
        self._mk_task(
            "syncer.sync_repo_since",
            {
                "repo": "o/r",
                "discovered": 5,
                "enqueued": 3,
                "rate_limit": {"cost": 10},
                "rate_events": [{"label": "repo_discovery", "cost": 10}],
            },
        )
        self._mk_task(
            "syncer.harvest_commit_history",
            {
                "repo": "o/r",
                "rate_events": [{"label": "commit_history_page", "cost": 7}],
            },
        )
        self._mk_task(
            "syncer.backfill_repo_history",
            {
                "repo": "o/r",
                "rate_limit": {"cost": 5},
            },
        )
        self._mk_task(
            "syncer.refresh_pending_ci_for_repo",
            {
                "repo": "o/r",
                "rate_events": [],
                "rate_limit": {"cost": 3},
            },
        )
        self._mk_task(
            "syncer.sync_ci_for_shas",
            {
                "repo": "o/r",
                "rate_events": [{"label": "ci_page", "cost": 4}],
            },
        )
        # Non-dict result should be ignored for token cost aggregation
        self._mk_task("syncer.collect_convergence", 5)

        res = collect_metrics_task()
        self.assertIn("id", res)
        snap = SyncerMetricsSnapshot.objects.get(id=res["id"])  # type: ignore[index]
        # Validate a few aggregates
        self.assertEqual(snap.pr_tasks, 1)
        self.assertEqual(snap.pr_token_cost, 50)
        self.assertEqual(snap.token_cost_total, 79)
        self.assertEqual(snap.repo_discovered, 5)
        self.assertEqual(snap.repo_enqueued, 3)
