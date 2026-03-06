from __future__ import annotations

import hashlib
from typing import Sequence

from syncer.services.rate_budget import _get_redis_client


TASK_ENQUEUE_DEDUPE_PREFIX = "syncer:dedupe:enqueue:"
TASK_RUNTIME_DEDUPE_PREFIX = "syncer:dedupe:runtime:"


def _normalize_shas(shas: Sequence[str]) -> list[str]:
    """Return sorted unique non-empty SHAs, normalized to lowercase."""
    seen: set[str] = set()
    normalized: list[str] = []
    for raw in shas:
        sha = (raw or "").strip().lower()
        if not sha or sha in seen:
            continue
        seen.add(sha)
        normalized.append(sha)
    normalized.sort()
    return normalized


def sync_pr_enqueue_key(*, repo_id: int, number: int) -> str:
    """Build enqueue dedupe identity key for sync_pr."""
    return f"{TASK_ENQUEUE_DEDUPE_PREFIX}sync_pr:{int(repo_id)}:{int(number)}"


def sync_pr_runtime_key(*, repo_id: int, number: int) -> str:
    """Build runtime dedupe identity key for sync_pr task execution."""
    return f"{TASK_RUNTIME_DEDUPE_PREFIX}sync_pr:{int(repo_id)}:{int(number)}"


def sync_ci_enqueue_key(
    *,
    repo_id: int,
    number: int,
    shas: Sequence[str],
    max_pages_per_sha: int | None,
) -> str:
    """Build enqueue dedupe identity key for sync_ci_for_shas."""
    pages = int(max_pages_per_sha) if max_pages_per_sha is not None else 0
    canonical_shas = ",".join(_normalize_shas(shas))
    digest = hashlib.sha1(canonical_shas.encode("utf-8")).hexdigest()[:16]
    return f"{TASK_ENQUEUE_DEDUPE_PREFIX}sync_ci:{int(repo_id)}:{int(number)}:{pages}:{digest}"


def claim_enqueue_slot(*, key: str, ttl_seconds: int) -> bool:
    """Return True if enqueue should proceed for this dedupe key.

    Uses Redis SET key value NX EX ttl. The first contender wins; duplicates are
    suppressed while the key lives. Redis errors fail open to avoid dropping work.
    """
    client = _get_redis_client()
    if client is None:
        return True
    try:
        ttl = max(1, int(ttl_seconds))
    except Exception:
        ttl = 1
    try:
        return bool(client.set(str(key), "1", nx=True, ex=ttl))
    except Exception:
        return True


def claim_runtime_slot(*, key: str, ttl_seconds: int) -> bool:
    """Return True if runtime execution should proceed for this dedupe key."""
    return claim_enqueue_slot(key=key, ttl_seconds=ttl_seconds)
