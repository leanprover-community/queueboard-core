from __future__ import annotations

"""
Rate budget coordination helpers using Redis.

Overview
--------
We coordinate GitHub GraphQL token usage across multiple Celery workers by writing the
most recent `rateLimit` snapshot observed (from any request) into Redis. Repo-level
orchestration tasks consult that snapshot to decide whether to stop early and schedule
a continuation at `resetAt`.

Expected behavior
-----------------
1) Every GraphQL call returns a `rateLimit { remaining, resetAt, cost, used }` snapshot.
2) `GitHubClient.execute(...)` writes this snapshot via `set_rate_snapshot` (best-effort).
3) `sync_repo_since_task` (repo-level) reads the snapshot. If `remaining <= threshold`, it
   stops early and schedules a continuation at `resetAt + jitter`, using `debounce_repo_schedule`
   to avoid duplicate schedules across processes.

Simple flow (ASCII)
-------------------

  [Celery beat]
        |
        v
  sync_active_repos  --->  sync_repo_since(repo)
                                 |
                                 |   GraphQL discovery
                                 v
                            [rate snapshot]
                                 |
         remaining <= threshold? +----- yes ----> schedule resume at resetAt (+jitter)
                                 |
                                 no
                                 |
                                 v
                       enqueue per-PR sync tasks

Implementation notes
--------------------
- Redis client is lazily created from `settings.CELERY_BROKER_URL` (reusing the Redis
  service that Celery already uses). All writes are best-effort; sync continues even
  if Redis is unavailable.
- Snapshots are stored under a token-scoped key with a TTL slightly past resetAt.
- Continuation schedules are debounced per repo+resetAt via SETNX with TTL.
"""

from datetime import datetime, timedelta, timezone
import hashlib
import json
import random
import time
from typing import Any, Dict, Optional, Sequence

from django.conf import settings


try:  # pragma: no cover - imported for side effects; calls are tested via mocks
    import redis
except Exception:  # pragma: no cover - environment without redis client
    redis = None  # type: ignore


RATE_SNAPSHOT_KEY = "gh:rate:snapshot"
RATE_SNAPSHOT_PREFIX = RATE_SNAPSHOT_KEY
SCHEDULE_KEY_PREFIX = "gh:rate:continue:repo:"
THROTTLE_SLOT_KEY = "gh:throttle:slot"


def _get_redis_client():  # pragma: no cover - exercised via higher-level tests
    if redis is None:
        return None
    url = getattr(settings, "CELERY_BROKER_URL", None)
    if not url:
        return None
    try:
        # Support both redis:// and rediss:// so TLS brokers (e.g., Heroku) work.
        if str(url).startswith(("redis://", "rediss://")):
            return redis.Redis.from_url(url, ssl=str(url).startswith("rediss://"))
        return None
    except Exception:
        return None


def token_fingerprint(token: str) -> str:
    """Return a stable, non-secret fingerprint for a token."""
    return hashlib.sha1(token.encode("utf-8")).hexdigest()[:16]


def _snapshot_key(token_id: Optional[str]) -> str:
    return f"{RATE_SNAPSHOT_PREFIX}:{token_id}" if token_id else RATE_SNAPSHOT_PREFIX


def set_rate_snapshot(rl: Dict[str, Any], token_id: Optional[str] = None) -> None:
    """Persist the latest `rateLimit` snapshot to Redis with a TTL.

    The payload is stored as JSON at a token-scoped key with an expiry chosen as:
    - if `resetAt` is present: TTL = seconds until resetAt + 3600s grace
    - otherwise: fixed TTL of 7200s (2h)
    """
    client = _get_redis_client()
    if client is None:
        return
    try:
        remaining = rl.get("remaining")
        reset_at = rl.get("resetAt")
        used = rl.get("used")
        cost = rl.get("cost")
        now = datetime.now(timezone.utc)
        payload = {
            "remaining": remaining,
            "resetAt": reset_at,
            "used": used,
            "cost": cost,
            "updated_at": now.isoformat(),
        }

        ttl = 7200  # default 2h
        if isinstance(reset_at, str):
            try:
                rdt = datetime.fromisoformat(reset_at.replace("Z", "+00:00"))
                if rdt.tzinfo is None:
                    rdt = rdt.replace(tzinfo=timezone.utc)
                # +1 hour safety
                ttl = max(60, int((rdt - now).total_seconds()) + 3600)
            except Exception:
                pass
        client.set(_snapshot_key(token_id), json.dumps(payload), ex=ttl)
    except Exception:
        # Best-effort: ignore Redis errors.
        return


def get_rate_snapshot(token_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Return the most recent rateLimit snapshot from Redis, or None if unavailable."""
    client = _get_redis_client()
    if client is None:
        return None
    try:
        raw = client.get(_snapshot_key(token_id))
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
        return None
    except Exception:
        return None


def get_rate_snapshots(token_ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """Return rateLimit snapshots for the provided token ids (best-effort)."""
    client = _get_redis_client()
    if client is None:
        return {}
    tids = list(dict.fromkeys(token_ids))  # preserve order, dedupe
    try:
        keys = [_snapshot_key(tid) for tid in tids]
        raws = client.mget(keys)
    except Exception:
        return {}

    out: Dict[str, Dict[str, Any]] = {}
    for tid, raw in zip(tids, raws):
        if not raw:
            continue
        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            data = json.loads(raw)
            if isinstance(data, dict):
                out[tid] = data
        except Exception:
            continue
    return out


def choose_token(tokens: Sequence[str]) -> Optional[str]:
    """Pick a token based on cached rate snapshots, falling back to random.

    Preference order:
    1) Token with the highest remaining above the SYNCER_RATE_REMAINING_MIN threshold.
    2) Token with the highest remaining > 0 (even if below threshold).
    3) Random choice among tokens without snapshots.
    4) Token with the soonest resetAt among exhausted tokens.
    """
    tokens = [t for t in tokens if t]
    if not tokens:
        return None

    token_ids = {token_fingerprint(t): t for t in tokens}
    snapshots = get_rate_snapshots(list(token_ids.keys()))
    now = datetime.now(timezone.utc)
    threshold = int(getattr(settings, "SYNCER_RATE_REMAINING_MIN", 200))

    above_threshold: list[tuple[int, str]] = []
    low_budget: list[tuple[int, str]] = []
    unknown: list[str] = []
    exhausted: list[tuple[datetime, str]] = []

    for tid, tok in token_ids.items():
        snap = snapshots.get(tid)
        if not snap:
            unknown.append(tok)
            continue
        rem = snap.get("remaining")
        reset_at = snap.get("resetAt")
        reset_dt: Optional[datetime] = None
        if isinstance(reset_at, str):
            try:
                reset_dt = datetime.fromisoformat(reset_at.replace("Z", "+00:00"))
                if reset_dt.tzinfo is None:
                    reset_dt = reset_dt.replace(tzinfo=timezone.utc)
            except Exception:
                reset_dt = None

        if isinstance(rem, int):
            if rem <= 0 and reset_dt and reset_dt > now:
                if reset_dt:
                    exhausted.append((reset_dt, tok))
                continue
            if rem > threshold:
                above_threshold.append((rem, tok))
            elif rem > 0:
                low_budget.append((rem, tok))
            else:
                if reset_dt:
                    exhausted.append((reset_dt, tok))
        else:
            unknown.append(tok)

    if above_threshold:
        return max(above_threshold, key=lambda x: x[0])[1]
    if low_budget:
        return max(low_budget, key=lambda x: x[0])[1]
    if unknown:
        return random.choice(unknown)
    if exhausted:
        # Pick the one that resets soonest
        return min(exhausted, key=lambda x: x[0])[1]
    return random.choice(tokens)


def debounce_repo_schedule(repo_id: int, reset_at_iso: str, ttl_seconds: int = 7200) -> bool:
    """Return True if we should schedule a continuation for this repo/resetAt.

    Uses an atomic SETNX on a dedupe key with expiration. Multiple contenders racing to
    schedule the same continuation will result in exactly one True (the first writer).
    """
    client = _get_redis_client()
    if client is None:
        return True  # without Redis, allow scheduling; advisory lock prevents overlap later
    key = f"{SCHEDULE_KEY_PREFIX}{int(repo_id)}:{reset_at_iso}"
    try:
        # SET key value NX EX ttl
        return bool(client.set(key, "1", nx=True, ex=int(max(60, ttl_seconds))))
    except Exception:
        return True  # fail-open to avoid deadlocks


def throttle_request_slot(interval_ms: int, max_wait_ms: int) -> None:
    """Gate GitHub requests so only one proceeds per interval across workers.

    Uses Redis SET NX with a short TTL to space requests. If Redis is unavailable,
    falls back to a simple sleep (local process only). The wait loop is capped by
    ``max_wait_ms`` to avoid blocking indefinitely when something goes wrong.
    """
    try:
        interval = max(0, int(interval_ms))
        if interval <= 0:
            return
        max_wait = max(interval, int(max_wait_ms))
    except Exception:
        return

    client = _get_redis_client()
    delay = interval / 1000
    deadline = time.monotonic() + max_wait / 1000

    if client is None:
        time.sleep(delay)
        return

    # Spin with a small sleep until we acquire the slot or hit the deadline.
    while True:
        try:
            if client.set(THROTTLE_SLOT_KEY, "1", nx=True, px=interval):
                return
        except Exception:
            # Fail open if Redis misbehaves; avoid blocking requests entirely.
            return

        if time.monotonic() >= deadline:
            return
        time.sleep(min(delay, 0.05 + delay / 2))
