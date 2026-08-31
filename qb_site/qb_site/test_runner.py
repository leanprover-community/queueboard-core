"""Test runner that isolates the Redis state tests share with the running dev stack.

``syncer.services.task_dedupe`` and ``syncer.services.rate_budget`` keep short-TTL keys in the
Celery broker's Redis, keyed by ``(repo_id, pr_number)``. Django destroys and recreates the test
database on every run, so those ids restart from 1 and collide with keys left behind by the
*previous* run: ``sync_pr`` then returns ``runtime_deduped`` / ``recently_processed`` and every
test that expected the task to do real work fails.

That failure mode is unusually expensive to diagnose, which is why this runner exists rather than
a note in a README. The symptoms actively mislead:

* the failures land in suites that have nothing to do with dedupe (the backfill tests),
* they appear only after a few consecutive runs, so the first run of a session is green,
* each failing test passes when run on its own, which reads as flakiness rather than shared state,
* and nothing in the diff is responsible, so the natural first suspicion is your own change.

Two independent guards, because either alone leaves a gap:

1. ``ci.py`` points the broker at its own Redis database index, so a test run cannot write into
   the keyspace a developer's ``docker compose up`` stack is using.
2. This runner clears the app's own namespaces before the suite starts, which is what actually
   breaks the run-to-run chain (guard 1 alone still reuses one index across runs).

Deliberately scan-and-delete over ``FLUSHDB``: it stays correct if someone runs the suite against
a shared index anyway, and it can never destroy data this project did not write.
"""

from __future__ import annotations

import logging

from django.test.runner import DiscoverRunner

log = logging.getLogger(__name__)

# Every prefix the app writes to the broker's Redis. Keep in sync with
# `syncer.services.task_dedupe` and `syncer.services.rate_budget`; the guard test in
# `qb_site/syncer/tests/test_redis_isolation.py` fails if a new prefix appears without being listed here.
SHARED_REDIS_KEY_PATTERNS: tuple[str, ...] = (
    "syncer:dedupe:*",
    "gh:rate:*",
    "gh:throttle:*",
)


def clear_shared_redis_state() -> int:
    """Delete the app's Redis keys, returning how many were removed.

    Never raises: Redis is optional for large parts of the suite, and a cleanup failure must not
    be the reason a test run cannot start.
    """
    try:
        from syncer.services.rate_budget import _get_redis_client

        client = _get_redis_client()
    except Exception:  # pragma: no cover - redis-py missing or broker misconfigured
        return 0
    if client is None:
        return 0

    removed = 0
    try:
        for pattern in SHARED_REDIS_KEY_PATTERNS:
            keys = list(client.scan_iter(match=pattern, count=500))
            if keys:
                removed += int(client.delete(*keys) or 0)
    except Exception as exc:  # pragma: no cover - unreachable broker
        log.warning("test runner: could not clear shared Redis state (%s)", exc)
        return removed
    return removed


class IsolatedRedisDiscoverRunner(DiscoverRunner):
    """``DiscoverRunner`` that clears shared Redis state before the suite runs."""

    def setup_test_environment(self, **kwargs) -> None:
        super().setup_test_environment(**kwargs)
        removed = clear_shared_redis_state()
        if removed:
            log.info("test runner: cleared %d leaked Redis key(s) before the suite", removed)
