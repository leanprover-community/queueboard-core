from __future__ import annotations

from unittest import mock
from datetime import datetime, timezone
import json

from django.test import TestCase

from syncer.services import rate_budget as rb


class TestRateBudget(TestCase):
    def test_set_and_get_snapshot(self) -> None:
        fake = mock.Mock()
        token_id = "abc123"
        # Force _get_redis_client to return our fake client
        with mock.patch.object(rb, "_get_redis_client", return_value=fake):
            now = datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)
            rl = {"remaining": 1234, "resetAt": "2025-01-01T01:00:00Z", "used": 100, "cost": 1}
            with mock.patch("syncer.services.rate_budget.datetime") as mdt:
                mdt.now.return_value = now
                mdt.timezone = timezone
                rb.set_rate_snapshot(rl, token_id=token_id)
        # Ensure set was called with JSON payload and an expiry
        args, kwargs = fake.set.call_args
        self.assertEqual(args[0], f"{rb.RATE_SNAPSHOT_PREFIX}:{token_id}")
        self.assertIn("remaining", kwargs["value"] if "value" in kwargs else args[1])

        # Simulate get
        data = {"remaining": 50, "resetAt": "2025-01-01T01:00:00Z", "used": 10, "cost": 1, "updated_at": now.isoformat()}
        fake.get.return_value = json.dumps(data).encode("utf-8")

        with mock.patch.object(rb, "_get_redis_client", return_value=fake):
            snap = rb.get_rate_snapshot(token_id=token_id)
        self.assertEqual(snap["remaining"], 50)

    def test_choose_token_prefers_highest_remaining(self) -> None:
        tokens = ["t1", "t2", "t3"]
        tids = [rb.token_fingerprint(t) for t in tokens]
        fake = mock.Mock()
        # Snapshots: t1 high, t2 low, t3 missing
        fake.mget.return_value = [
            json.dumps({"remaining": 500, "resetAt": "2025-01-01T01:00:00Z"}).encode("utf-8"),
            json.dumps({"remaining": 5, "resetAt": "2025-01-01T01:00:00Z"}).encode("utf-8"),
            None,
        ]
        with mock.patch.object(rb, "_get_redis_client", return_value=fake):
            chosen = rb.choose_token(tokens)
        self.assertEqual(chosen, "t1")
        fake.mget.assert_called_once()
        args, _ = fake.mget.call_args
        # Ensure we requested all token keys
        for tid in tids:
            self.assertIn(f"{rb.RATE_SNAPSHOT_PREFIX}:{tid}", args[0])

    def test_choose_token_falls_back_to_unknown_random(self) -> None:
        tokens = ["t1", "t2"]
        fake = mock.Mock()
        fake.mget.return_value = [None, None]
        with mock.patch.object(rb, "_get_redis_client", return_value=fake):
            with mock.patch("syncer.services.rate_budget.random.choice", return_value="t2") as mchoice:
                chosen = rb.choose_token(tokens)
        self.assertEqual(chosen, "t2")
        mchoice.assert_called_once_with(tokens)

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
