from __future__ import annotations

from unittest import mock
from datetime import datetime, timezone
import json

from django.test import TestCase

from syncer.services import rate_budget as rb


class TestRateBudget(TestCase):
    def test_set_and_get_snapshot(self) -> None:
        fake = mock.Mock()
        # Force _get_redis_client to return our fake client
        with mock.patch.object(rb, "_get_redis_client", return_value=fake):
            now = datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)
            rl = {"remaining": 1234, "resetAt": "2025-01-01T01:00:00Z", "used": 100, "cost": 1}
            with mock.patch("syncer.services.rate_budget.datetime") as mdt:
                mdt.now.return_value = now
                mdt.timezone = timezone
                rb.set_rate_snapshot(rl)
        # Ensure set was called with JSON payload and an expiry
        args, kwargs = fake.set.call_args
        self.assertEqual(args[0], rb.RATE_SNAPSHOT_KEY)
        self.assertIn("remaining", kwargs["value"] if "value" in kwargs else args[1])

        # Simulate get
        data = {"remaining": 50, "resetAt": "2025-01-01T01:00:00Z", "used": 10, "cost": 1, "updated_at": now.isoformat()}
        fake.get.return_value = json.dumps(data).encode("utf-8")

        with mock.patch.object(rb, "_get_redis_client", return_value=fake):
            snap = rb.get_rate_snapshot()
        self.assertEqual(snap["remaining"], 50)

    def test_debounce_repo_schedule(self) -> None:
        fake = mock.Mock()
        with mock.patch.object(rb, "_get_redis_client", return_value=fake):
            # First call should set (nx=True returns True)
            fake.set.return_value = True
            ok1 = rb.debounce_repo_schedule(7, "2025-01-01T00:00:00Z")
            # Second call should not set (nx=True returns False)
            fake.set.return_value = False
            ok2 = rb.debounce_repo_schedule(7, "2025-01-01T00:00:00Z")
        self.assertTrue(ok1)
        self.assertFalse(ok2)
