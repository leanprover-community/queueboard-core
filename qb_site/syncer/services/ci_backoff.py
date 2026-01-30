from __future__ import annotations

from django.conf import settings
from django.utils import timezone

from syncer.models import CIShaFetchState, PullRequest


def should_enqueue_ci_sha(*, pr: PullRequest, sha: str, reason: str | None = None) -> bool:
    """Return True if CI-by-SHA should be enqueued.

    Note: "skipped_association" is treated as a no-op result and does not backoff.
    This path should be deprecated once CI is SHA-keyed instead of PR-keyed.
    """
    _ = reason
    if not sha:
        return False
    state = CIShaFetchState.objects.filter(repository=pr.repository, sha=sha).first()
    if state is None:
        return True
    last = state.last_result or ""
    if last in {"not_found", "filtered"}:
        return False
    if last == "skipped_association":
        return True
    now = timezone.now()
    age = now - state.last_attempted_at
    if last == "empty":
        cooldown = int(getattr(settings, "SYNCER_CI_SHA_BACKOFF_EMPTY_SECONDS", 300))
        return age.total_seconds() >= cooldown
    if last == "error":
        cooldown = int(getattr(settings, "SYNCER_CI_SHA_BACKOFF_ERROR_SECONDS", 300))
        return age.total_seconds() >= cooldown
    return True


def record_ci_sha_fetch(
    *,
    pr: PullRequest,
    sha: str,
    result: str,
    now: timezone.datetime | None = None,
) -> None:
    """Persist CI-by-SHA fetch attempt results for backoff decisions."""
    if not sha:
        return
    now_ts = now or timezone.now()
    updates = {
        "last_attempted_at": now_ts,
        "last_result": result,
    }
    if result == "ok":
        updates["last_success_at"] = now_ts
    state, created = CIShaFetchState.objects.get_or_create(
        repository=pr.repository,
        sha=sha,
        defaults={
            **updates,
            "attempts": 1,
        },
    )
    if created:
        return
    state.attempts = int(state.attempts or 0) + 1
    for field, value in updates.items():
        setattr(state, field, value)
    state.save(update_fields=["attempts", "last_attempted_at", "last_result", "last_success_at", "updated_at"])
