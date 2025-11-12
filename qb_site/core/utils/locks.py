from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from django.db import connection


# Namespace key for syncer per-repo advisory locks. Chosen arbitrarily but stable.
LOCK_NS_SYNCER_REPO = 0x73796E63  # ASCII 'sync'


@contextmanager
def repo_advisory_lock(repo_id: int) -> Iterator[bool]:
    """Try to acquire a Postgres advisory lock for a repository.

    Uses a two-key advisory lock (namespace, repo_id). Returns a boolean indicating
    whether the lock was acquired. If acquired, releases it on exit.
    """
    acquired = False
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s, %s)", [LOCK_NS_SYNCER_REPO, int(repo_id)])
        row = cursor.fetchone()
        if row and isinstance(row[0], bool):
            acquired = row[0]
        else:  # pragma: no cover - defensive
            acquired = False
    try:
        yield acquired
    finally:
        if acquired:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s, %s)", [LOCK_NS_SYNCER_REPO, int(repo_id)])
