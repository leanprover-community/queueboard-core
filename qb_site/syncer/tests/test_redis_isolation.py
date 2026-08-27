"""Guards for the shared-Redis test isolation (see `qb_site/qb_site/test_runner.py`).

The bug these protect against: dedupe keys are `(repo_id, pr_number)`-scoped, the test database
restarts ids from 1 on every run, so a second run collides with the first run's leftover keys and
unrelated suites start failing. Nothing in the diff is responsible, which is what makes it costly.
"""

from __future__ import annotations

import re
from pathlib import Path

from django.test import TestCase

from qb_site.test_runner import SHARED_REDIS_KEY_PATTERNS, clear_shared_redis_state
from syncer.services.rate_budget import _get_redis_client
from syncer.services.task_dedupe import TASK_ENQUEUE_DEDUPE_PREFIX, TASK_RUNTIME_DEDUPE_PREFIX

# Modules allowed to write to the broker's Redis. A key literal appearing here must be covered by
# SHARED_REDIS_KEY_PATTERNS, or the runner will leave it behind and the leak returns.
_KEY_WRITING_MODULES = ("syncer/services/task_dedupe.py", "syncer/services/rate_budget.py")
# "syncer:dedupe:runtime:", "gh:rate:snapshot", ... — a colon-separated lowercase literal.
_KEY_LITERAL = re.compile(r'"([a-z][a-z0-9_]*(?::[a-z0-9_]+)+:?)"')


def _pattern_covers(pattern: str, key: str) -> bool:
    return re.fullmatch(re.escape(pattern).replace(r"\*", ".*"), key) is not None


class TestSharedRedisKeyCoverage(TestCase):
    """Every namespace the app writes must be one the runner clears."""

    def test_declared_dedupe_prefixes_are_covered(self) -> None:
        for prefix in (TASK_ENQUEUE_DEDUPE_PREFIX, TASK_RUNTIME_DEDUPE_PREFIX):
            probe = f"{prefix}probe:1:2"
            self.assertTrue(
                any(_pattern_covers(p, probe) for p in SHARED_REDIS_KEY_PATTERNS),
                f"{prefix!r} is not covered by SHARED_REDIS_KEY_PATTERNS",
            )

    def test_no_key_literal_escapes_the_patterns(self) -> None:
        root = Path(__file__).resolve().parents[2]
        uncovered: list[str] = []
        for rel in _KEY_WRITING_MODULES:
            for key in _KEY_LITERAL.findall((root / rel).read_text()):
                probe = key if not key.endswith(":") else f"{key}probe"
                if not any(_pattern_covers(p, probe) for p in SHARED_REDIS_KEY_PATTERNS):
                    uncovered.append(f"{rel}: {key!r}")
        self.assertEqual(
            uncovered,
            [],
            "These Redis key literals are not cleared between test runs. Add a matching entry to "
            "SHARED_REDIS_KEY_PATTERNS in qb_site/qb_site/test_runner.py:\n  " + "\n  ".join(uncovered),
        )


class TestClearSharedRedisState(TestCase):
    """The clear itself works, and stays inside the app's own namespaces.

    Skipped where Redis is unreachable, which includes `scripts/repo_check_compose.sh`: it starts
    `web` against `db` only, so `redis:6379` does not resolve. That is also why the leak these
    guards prevent never bit the canonical script — with no Redis, dedupe always fails open. It
    bites the workflows that *do* have Redis up: a full `docker compose up`, or the focused
    host-test loop in this directory's AGENTS.md.
    """

    def _reachable_client(self):
        """Return a live Redis client, or skip. `_get_redis_client()` connects lazily, so it
        returns an object even when nothing is listening — a `None` check alone is not enough."""
        client = _get_redis_client()
        if client is None:
            self.skipTest("no Redis broker configured")
        try:
            client.ping()
        except Exception as exc:
            self.skipTest(f"Redis broker not reachable ({exc})")
        return client

    def test_clears_app_keys_and_leaves_others_alone(self) -> None:
        client = self._reachable_client()
        bystander = "someone-elses-key:test-redis-isolation"
        client.set(f"{TASK_RUNTIME_DEDUPE_PREFIX}sync_pr:1:11", "1", ex=300)
        client.set("gh:rate:snapshot", "1", ex=300)
        client.set(bystander, "1", ex=300)
        try:
            removed = clear_shared_redis_state()
            self.assertGreaterEqual(removed, 2)
            self.assertIsNone(client.get(f"{TASK_RUNTIME_DEDUPE_PREFIX}sync_pr:1:11"))
            self.assertIsNone(client.get("gh:rate:snapshot"))
            # Scan-and-delete, never FLUSHDB: anything this project did not write must survive.
            self.assertIsNotNone(client.get(bystander))
        finally:
            client.delete(bystander)

    def test_is_safe_when_redis_is_unavailable(self) -> None:
        # Cleanup failure must never be the reason a suite cannot start.
        with self.settings(CELERY_BROKER_URL=""):
            self.assertEqual(clear_shared_redis_state(), 0)
