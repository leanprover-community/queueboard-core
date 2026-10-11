from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from syncer.models import GitHubWebhookDelivery, GitHubWebhookDeliveryStatus
from syncer.tasks.webhook_delivery_cleanup import expire_old_webhook_deliveries_task


def _delivery(delivery_id: str, *, age: timedelta) -> GitHubWebhookDelivery:
    row = GitHubWebhookDelivery.objects.create(
        delivery_id=delivery_id,
        event_type="pull_request",
        status=GitHubWebhookDeliveryStatus.ACCEPTED,
    )
    # received_at is auto_now_add; backdate it with an update.
    GitHubWebhookDelivery.objects.filter(id=row.id).update(received_at=timezone.now() - age)
    return row


def _delete_statements(ctx: CaptureQueriesContext) -> int:
    return sum(1 for q in ctx.captured_queries if q["sql"].lstrip().upper().startswith("DELETE"))


class ExpireOldWebhookDeliveriesTaskTests(TestCase):
    def setUp(self) -> None:
        self.old = [_delivery(f"old-{i}", age=timedelta(days=10 + i)) for i in range(5)]
        self.recent = [_delivery(f"new-{i}", age=timedelta(days=1)) for i in range(2)]

    def _remaining_ids(self) -> set[str]:
        return set(GitHubWebhookDelivery.objects.values_list("delivery_id", flat=True))

    def test_deletes_only_rows_older_than_retention(self) -> None:
        result = expire_old_webhook_deliveries_task(retention_days=7, max_rows=0)

        self.assertEqual(result["deleted"], 5)
        self.assertFalse(result["capped"])
        self.assertEqual(self._remaining_ids(), {"new-0", "new-1"})

    def test_deletes_in_batches(self) -> None:
        with patch("syncer.tasks.webhook_delivery_cleanup._DELETE_BATCH_SIZE", 2):
            with CaptureQueriesContext(connection) as ctx:
                result = expire_old_webhook_deliveries_task(retention_days=7, max_rows=0)

        self.assertEqual(result["deleted"], 5)
        self.assertEqual(_delete_statements(ctx), 3)  # 2 + 2 + 1
        self.assertEqual(self._remaining_ids(), {"new-0", "new-1"})

    def test_cap_stops_the_run_and_the_next_run_finishes(self) -> None:
        with patch("syncer.tasks.webhook_delivery_cleanup._DELETE_BATCH_SIZE", 2):
            first = expire_old_webhook_deliveries_task(retention_days=7, max_rows=3)
            # Oldest-first is by id; the first three rows created go first.
            self.assertEqual(first["deleted"], 3)
            self.assertTrue(first["capped"])
            self.assertEqual(self._remaining_ids(), {"old-3", "old-4", "new-0", "new-1"})

            second = expire_old_webhook_deliveries_task(retention_days=7, max_rows=3)
            self.assertEqual(second["deleted"], 2)
            self.assertFalse(second["capped"])
            self.assertEqual(self._remaining_ids(), {"new-0", "new-1"})

    @override_settings(SYNCER_WEBHOOK_DELIVERY_CLEANUP_MAX_ROWS=4, SYNCER_WEBHOOK_DELIVERY_RETENTION_DAYS=7)
    def test_reads_cap_and_retention_from_settings(self) -> None:
        result = expire_old_webhook_deliveries_task()

        self.assertEqual(result["retention_days"], 7)
        self.assertEqual(result["max_rows"], 4)
        self.assertEqual(result["deleted"], 4)
        self.assertTrue(result["capped"])

    def test_nothing_to_delete(self) -> None:
        with CaptureQueriesContext(connection) as ctx:
            result = expire_old_webhook_deliveries_task(retention_days=30, max_rows=0)

        self.assertEqual(result["deleted"], 0)
        self.assertFalse(result["capped"])
        self.assertEqual(_delete_statements(ctx), 0)
        self.assertEqual(len(self._remaining_ids()), 7)
