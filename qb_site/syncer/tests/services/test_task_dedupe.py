from __future__ import annotations

from unittest import mock

from django.test import TestCase

from syncer.services import task_dedupe as td


class TestTaskDedupe(TestCase):
    def test_sync_pr_enqueue_key(self) -> None:
        key = td.sync_pr_enqueue_key(repo_id=12, number=34)
        self.assertEqual(key, "syncer:dedupe:enqueue:sync_pr:12:34")

    def test_sync_ci_enqueue_key_normalizes_shas(self) -> None:
        key1 = td.sync_ci_enqueue_key(
            repo_id=5,
            number=7,
            shas=["ABC", "def", "abc", " "],
            max_pages_per_sha=1,
        )
        key2 = td.sync_ci_enqueue_key(
            repo_id=5,
            number=7,
            shas=["def", "abc"],
            max_pages_per_sha=1,
        )
        self.assertEqual(key1, key2)

    def test_claim_enqueue_slot_first_wins(self) -> None:
        fake = mock.Mock()
        with mock.patch.object(td, "_get_redis_client", return_value=fake):
            fake.set.return_value = True
            first = td.claim_enqueue_slot(key="k1", ttl_seconds=30)
            fake.set.return_value = False
            second = td.claim_enqueue_slot(key="k1", ttl_seconds=30)

        self.assertTrue(first)
        self.assertFalse(second)

    def test_claim_enqueue_slot_fail_open_on_missing_redis(self) -> None:
        with mock.patch.object(td, "_get_redis_client", return_value=None):
            ok = td.claim_enqueue_slot(key="k1", ttl_seconds=30)
        self.assertTrue(ok)

    def test_claim_enqueue_slot_fail_open_on_redis_error(self) -> None:
        fake = mock.Mock()
        fake.set.side_effect = RuntimeError("redis unavailable")
        with mock.patch.object(td, "_get_redis_client", return_value=fake):
            ok = td.claim_enqueue_slot(key="k1", ttl_seconds=30)
        self.assertTrue(ok)
