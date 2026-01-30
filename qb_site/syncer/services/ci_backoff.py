from __future__ import annotations

from syncer.models import PullRequest


def should_enqueue_ci_sha(*, pr: PullRequest, sha: str, reason: str | None = None) -> bool:
    """Return True if CI-by-SHA should be enqueued.

    Placeholder hook for future backoff/ledger policy. Currently always True.
    """
    _ = reason
    return True
