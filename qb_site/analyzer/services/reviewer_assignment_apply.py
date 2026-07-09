"""Apply proposed reviewer assignments to GitHub.

This is the application half of reviewer auto-assignment. The producer
(``analyzer.refresh_reviewer_assignments``) stores advisory
``ReviewerAssignmentSnapshot`` payloads; this service reads the authoritative
*default rule set* snapshot per repo and POSTs the proposed assignees to GitHub
via :class:`GitHubAssignmentClient`, re-validating each proposal against live
state first and recording every outcome in :class:`ReviewerAssignmentApplication`.

It replaces the legacy GitHub Actions workflow (``scripts/assign_reviewers.py``),
which downloaded the legacy pipeline's ``automatic_assignments.json`` and assigned
without any idempotency tracking.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Callable

from django.utils import timezone as dj_timezone

from analyzer.models import ReviewerAssignmentApplication, ReviewerAssignmentSnapshot
from analyzer.services.queue_rules import default_rule_set_for_repo
from analyzer.services.reviewer_assignment import _active_reviewer_logins, _opt_outs_for_prs, build_reviewer_catalog
from analyzer.services.reviewer_assignment_engine import _normalize_login
from core.models import Repository
from core.services.github_assignment import AssignmentMutationError, GitHubAssignmentClient
from core.services.github_operation_tokens import resolve_github_app_operation_token
from syncer.models import PullRequest

log = logging.getLogger(__name__)

TokenResolver = Callable[..., "str | None"]
SyncEnqueuer = Callable[[str, str, int], None]


def _default_sync_enqueuer(owner: str, repo: str, number: int) -> None:
    """Enqueue a per-PR sync so the new assignee converges into our state."""
    repository = Repository.objects.filter(owner=owner, name=repo).only("id").first()
    if repository is None:
        return
    try:
        from syncer.tasks.sync_tasks import sync_pr_task

        sync_pr_task.delay(repository.id, int(number))
    except Exception:  # pragma: no cover - defensive enqueue guard
        log.warning(
            "reviewer_assignment_apply: post-apply sync enqueue failed",
            extra={"owner": owner, "repo": repo, "number": number},
        )


def _current_assignee_logins(live_pr: PullRequest | None) -> set[str]:
    if live_pr is None:
        return set()
    return {_normalize_login(str(login)) for login in (live_pr.assignees or []) if login}


def latest_default_snapshot(repository: Repository) -> tuple[ReviewerAssignmentSnapshot | None, str]:
    """Return the newest authoritative default-rule-set assignment snapshot + its cache key.

    Both the legacy apply sweep and the acceptance-gate propose step act on this same
    ``{pr_number: reviewer_login}`` producer output, so they resolve it identically here.
    """
    rule_set = default_rule_set_for_repo(repository)
    cache_key = str(rule_set.id) if rule_set else "default"
    snapshot = (
        ReviewerAssignmentSnapshot.objects.filter(repository=repository, cache_key=cache_key)
        .order_by("-generated_at", "-id")
        .first()
    )
    return snapshot, cache_key


def parse_snapshot_assignments(snapshot: ReviewerAssignmentSnapshot) -> list[tuple[int, str]]:
    """Parse ``payload["automatic_assignments"]`` into a sorted ``[(pr_number, login), ...]`` list."""
    raw = snapshot.payload.get("automatic_assignments", {}) or {}
    proposals: list[tuple[int, str]] = []
    for key, value in raw.items():
        try:
            pr_number = int(key)
        except (TypeError, ValueError):
            continue
        login = str(value).strip()
        if not login:
            continue
        proposals.append((pr_number, login))
    proposals.sort(key=lambda pair: pair[0])
    return proposals


def assign_reviewer_and_record(
    *,
    repository: Repository,
    pr_number: int,
    login: str,
    snapshot: ReviewerAssignmentSnapshot | None,
    run_date,
    token: str,
    assignment_client: GitHubAssignmentClient | None = None,
    sync_enqueuer: SyncEnqueuer = _default_sync_enqueuer,
) -> tuple[str, GitHubAssignmentClient | None, ReviewerAssignmentApplication | None]:
    """Execute the 046 direct-assign mutation for one already-validated ``(pr, login)``.

    Idempotently creates the PENDING ``ReviewerAssignmentApplication`` for
    ``(run_date, repo, pr, reviewer)``; if a row already exists returns
    ``("already_recorded", assignment_client, <existing record>)`` without mutating. The caller can
    inspect ``record.status`` to tell an already-APPLIED row from a prior FAILED/PENDING one —
    ``already_recorded`` does *not* imply the assignment ever landed. Otherwise POSTs the assignee
    via ``GitHubAssignmentClient`` (built from ``token`` when not supplied), confirms the login
    actually landed (GitHub silently drops unassignable logins), marks the row APPLIED/FAILED, and
    enqueues ``syncer.sync_pr`` on success. Returns ``(outcome, client, record)`` with ``outcome``
    in {"applied", "failed", "already_recorded"}.

    Shared verbatim by the legacy apply sweep (doc 046), the acceptance-gate propose step's
    auto/fallback direct-assign path, and the console accept handler (doc 050) so the GitHub
    mutation and the ``ReviewerAssignmentApplication`` audit trail stay identical across all three.
    """
    owner = repository.owner
    name = repository.name
    record, created = ReviewerAssignmentApplication.objects.get_or_create(
        run_date=run_date,
        repository=repository,
        pr_number=pr_number,
        reviewer_login=login,
        defaults={
            "snapshot": snapshot,
            "status": ReviewerAssignmentApplication.STATUS_PENDING,
        },
    )
    if not created:
        return ("already_recorded", assignment_client, record)

    if assignment_client is None:
        assignment_client = GitHubAssignmentClient(token=token)
    login_norm = _normalize_login(login)
    try:
        resulting_assignees = assignment_client.assign(owner=owner, repo=name, number=pr_number, github_login=login)
    except AssignmentMutationError as exc:
        record.status = ReviewerAssignmentApplication.STATUS_FAILED
        record.error = str(exc)[:2000]
        record.save(update_fields=["status", "error", "updated_at"])
        return ("failed", assignment_client, record)

    # GitHub's "add assignees" endpoint silently ignores logins that are not assignable (e.g. no
    # repo access): it returns 200 with the login absent rather than erroring. Confirm the login
    # actually landed before recording success, so we never claim an assignment that did not take.
    resulting_logins = {_normalize_login(assignee) for assignee in resulting_assignees if assignee}
    if login_norm not in resulting_logins:
        record.status = ReviewerAssignmentApplication.STATUS_FAILED
        record.error = (
            f"GitHub accepted the request but '{login}' was not in the resulting "
            f"assignee set {sorted(resulting_logins)} (likely not an assignable user)."
        )[:2000]
        record.save(update_fields=["status", "error", "updated_at"])
        return ("failed", assignment_client, record)

    record.status = ReviewerAssignmentApplication.STATUS_APPLIED
    record.applied_at = dj_timezone.now()
    record.error = ""
    record.save(update_fields=["status", "applied_at", "error", "updated_at"])
    sync_enqueuer(owner, name, pr_number)
    return ("applied", assignment_client, record)


def _empty_stats() -> dict[str, Any]:
    return {
        "candidates": 0,
        "applied": 0,
        "failed": 0,
        "skipped_already_assigned": 0,
        "skipped_opted_out": 0,
        "skipped_ineligible": 0,
        "skipped_recently_applied": 0,
        "skipped_no_token": 0,
        "skipped_disabled": 0,
        "skipped_dry_run": 0,
        "skipped_already_recorded": 0,
        "capped": False,
        "capped_remaining": 0,
    }


def apply_assignments_for_repo(
    repository: Repository,
    *,
    run_date,
    now: datetime,
    enabled: bool,
    dry_run: bool,
    dedupe_days: int,
    max_age_hours: int,
    max_per_repo: int,
    token_resolver: TokenResolver = resolve_github_app_operation_token,
    assignment_client: GitHubAssignmentClient | None = None,
    sync_enqueuer: SyncEnqueuer = _default_sync_enqueuer,
) -> dict[str, Any]:
    """Apply the latest default-rule-set assignment snapshot for ``repository``.

    Each proposed ``(pr_number -> reviewer_login)`` pair is re-validated against
    live state before mutating, and every decision is persisted to
    ``ReviewerAssignmentApplication`` (one row per ``(run_date, repo, pr, reviewer)``).
    Returns a concise summary dict for task aggregation / admin debugging.
    """
    owner = repository.owner
    name = repository.name
    result: dict[str, Any] = {
        "repo": f"{owner}/{name}",
        "repo_id": int(repository.id),
        "stats": _empty_stats(),
    }

    snapshot, cache_key = latest_default_snapshot(repository)
    result["cache_key"] = cache_key
    if snapshot is None:
        result["status"] = "skipped"
        result["reason"] = "no_snapshot"
        return result

    age_seconds = (now - snapshot.generated_at).total_seconds()
    result["snapshot_id"] = int(snapshot.id)
    result["snapshot_generated_at"] = snapshot.generated_at.isoformat()
    if max_age_hours > 0 and age_seconds > max_age_hours * 3600:
        result["status"] = "skipped"
        result["reason"] = "stale_snapshot"
        result["snapshot_age_hours"] = round(age_seconds / 3600, 2)
        return result

    proposals = parse_snapshot_assignments(snapshot)

    stats = result["stats"]
    stats["candidates"] = len(proposals)
    if not proposals:
        result["status"] = "ok"
        return result

    pr_numbers = [pr_number for pr_number, _ in proposals]
    eligible_logins = _active_reviewer_logins(build_reviewer_catalog(repository, now=now))
    opt_outs = _opt_outs_for_prs(repository, pr_numbers)
    # We deliberately trust the last-synced PR rows here rather than forcing a fresh
    # per-PR sync before mutating. The lag window is small and self-healing: opt-outs
    # are recomputed on every pr_sync, an assign/unassign bumps the PR's updatedAt so
    # the discovery sweep re-syncs it promptly, and `recently_applied` backstops the
    # convergence gap. A synchronous pre-apply sync per candidate would add a GitHub
    # round-trip and latency for marginal safety. See design doc 046.
    live_by_number = {
        int(pr.number): pr
        for pr in PullRequest.objects.filter(repository=repository, number__in=pr_numbers).only("number", "state", "assignees")
    }

    dedupe_cutoff = now - timedelta(days=dedupe_days) if dedupe_days > 0 else None
    recently_applied: set[tuple[int, str]] = set()
    if dedupe_cutoff is not None:
        rows = ReviewerAssignmentApplication.objects.filter(
            repository=repository,
            pr_number__in=pr_numbers,
            status=ReviewerAssignmentApplication.STATUS_APPLIED,
            applied_at__gte=dedupe_cutoff,
        ).values_list("pr_number", "reviewer_login")
        recently_applied = {(int(pr_number), _normalize_login(login)) for pr_number, login in rows}

    token: str | None = None
    token_attempted = False

    def _record(
        pr_number: int, login: str, status: str, *, applied_at: datetime | None = None, error: str = ""
    ) -> ReviewerAssignmentApplication | None:
        """Idempotently record one outcome row.

        Returns the newly created row, or ``None`` when a row for this
        ``(run_date, repo, pr, reviewer)`` already existed. A model instance is
        truthy and ``None`` is falsy, so callers can branch on the return value.
        """
        obj, created = ReviewerAssignmentApplication.objects.get_or_create(
            run_date=run_date,
            repository=repository,
            pr_number=pr_number,
            reviewer_login=login,
            defaults={
                "snapshot": snapshot,
                "status": status,
                "applied_at": applied_at,
                "error": error[:2000],
            },
        )
        if not created:
            stats["skipped_already_recorded"] += 1
            return None
        return obj

    for index, (pr_number, login) in enumerate(proposals):
        login_norm = _normalize_login(login)
        live_pr = live_by_number.get(pr_number)

        # --- Re-validate the proposal against live state (snapshot may be ~1 day old). ---
        if login_norm not in eligible_logins:
            if _record(pr_number, login, ReviewerAssignmentApplication.STATUS_SKIPPED_INELIGIBLE):
                stats["skipped_ineligible"] += 1
            continue
        if login_norm in opt_outs.get(pr_number, set()):
            if _record(pr_number, login, ReviewerAssignmentApplication.STATUS_SKIPPED_OPTED_OUT):
                stats["skipped_opted_out"] += 1
            continue
        not_open = live_pr is not None and str(live_pr.state).strip().lower() != "open"
        current_assignees = _current_assignee_logins(live_pr)
        # "Already assigned" also covers PRs that are no longer open (nothing to do).
        if not_open or (current_assignees & eligible_logins) or (login_norm in current_assignees):
            if _record(pr_number, login, ReviewerAssignmentApplication.STATUS_SKIPPED_ALREADY_ASSIGNED):
                stats["skipped_already_assigned"] += 1
            continue
        if (pr_number, login_norm) in recently_applied:
            if _record(pr_number, login, ReviewerAssignmentApplication.STATUS_SKIPPED_RECENTLY_APPLIED):
                stats["skipped_recently_applied"] += 1
            continue

        # --- Gating: dry-run / feature flag / token. ---
        # Dry-run takes precedence: it is the preview mode (typically enabled=False,
        # dry_run=True), so a proposal that would otherwise apply is recorded as
        # skipped_dry_run rather than skipped_disabled.
        if dry_run:
            if _record(pr_number, login, ReviewerAssignmentApplication.STATUS_SKIPPED_DRY_RUN):
                stats["skipped_dry_run"] += 1
            continue
        if not enabled:
            if _record(pr_number, login, ReviewerAssignmentApplication.STATUS_SKIPPED_DISABLED):
                stats["skipped_disabled"] += 1
            continue
        if not token_attempted:
            token = token_resolver(operation="assign_pr", owner=owner, repo=name)
            token_attempted = True
        if not token:
            if _record(pr_number, login, ReviewerAssignmentApplication.STATUS_SKIPPED_NO_TOKEN):
                stats["skipped_no_token"] += 1
            continue

        # --- Mutation path (subject to the per-repo cap). ---
        if max_per_repo > 0 and (stats["applied"] + stats["failed"]) >= max_per_repo:
            stats["capped"] = True
            stats["capped_remaining"] = len(proposals) - index
            log.info(
                "reviewer_assignment_apply: per-repo cap reached repo=%s/%s cap=%s remaining=%s",
                owner,
                name,
                max_per_repo,
                stats["capped_remaining"],
            )
            break

        outcome, assignment_client, _ = assign_reviewer_and_record(
            repository=repository,
            pr_number=pr_number,
            login=login,
            snapshot=snapshot,
            run_date=run_date,
            token=token,
            assignment_client=assignment_client,
            sync_enqueuer=sync_enqueuer,
        )
        if outcome == "applied":
            stats["applied"] += 1
        elif outcome == "failed":
            stats["failed"] += 1
        else:  # already_recorded: another writer created the row for this run first
            stats["skipped_already_recorded"] += 1

    result["status"] = "ok"
    return result


__all__ = [
    "apply_assignments_for_repo",
    "assign_reviewer_and_record",
    "latest_default_snapshot",
    "parse_snapshot_assignments",
]
