"""Propose reviewer assignments through the acceptance gate (design doc 050).

This is the batch half that the legacy ``apply_reviewer_assignments`` splits into. It reads the
authoritative default-rule-set ``ReviewerAssignmentSnapshot`` (the same producer output apply
consumed), re-validates each ``{pr_number: reviewer_login}`` pair against live state, then branches
on the reviewer's ``ReviewerPreference.assignment_acceptance`` mode:

- ``auto``                      -> direct-assign on GitHub (the verbatim 046 mutation path).
- ``confirm`` + reachable       -> create an ``AssignmentProposal`` awaiting acceptance (no GitHub
  write; the assignment is executed later by the console accept handler).
- ``confirm`` + *unreachable*   -> fall back to direct-assign. "Unreachable" means the reviewer has
  no Zulip link at all (``User.zulip_user_id`` is null), never merely that notifications are muted.

The mutation half (assign + ``ReviewerAssignmentApplication`` + ``sync_pr``) is reused verbatim via
``assign_reviewer_and_record`` so auto/fallback assignments and (Chunk 6) console accepts share one
implementation. Proposals are DB-only and therefore not counted against the GitHub per-repo cap.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from django.db import IntegrityError, transaction

from analyzer.models import AssignmentProposal, ReviewerAssignmentApplication
from analyzer.services.reviewer_assignment import _active_reviewer_logins, _opt_outs_for_prs, build_reviewer_catalog
from analyzer.services.reviewer_assignment_apply import (
    SyncEnqueuer,
    TokenResolver,
    _current_assignee_logins,
    _default_sync_enqueuer,
    assign_reviewer_and_record,
    latest_default_snapshot,
    parse_snapshot_assignments,
)
from analyzer.services.reviewer_assignment_engine import _normalize_login
from core.models import Repository, ReviewerPreference
from core.services.github_assignment import GitHubAssignmentClient
from core.services.github_operation_tokens import resolve_github_app_operation_token
from syncer.models import PullRequest

log = logging.getLogger(__name__)

MIN_WINDOW_DAYS = 7


def _empty_stats() -> dict[str, Any]:
    return {
        "candidates": 0,
        "proposed": 0,
        "assigned_auto": 0,
        "assigned_fallback": 0,
        "failed": 0,
        "skipped_already_proposed": 0,
        "skipped_already_assigned": 0,
        "skipped_opted_out": 0,
        "skipped_ineligible": 0,
        "skipped_recently_applied": 0,
        "skipped_no_token": 0,
        "skipped_dry_run": 0,
        "skipped_disabled": 0,
        "skipped_already_recorded": 0,
        "capped": False,
        "capped_remaining": 0,
    }


def _resolve_window_days(pref: ReviewerPreference, default: int) -> int:
    """Per-reviewer acceptance window; overrides are clamped to >= 7 days (design doc 050, "≥7").

    A reviewer may set ``notification_settings["assignment_proposal_window_days"]``; anything
    smaller than a week (or non-numeric) is coerced up to the weekly floor. The weekly floor
    applies only to per-reviewer overrides — the operator-configured global default
    (``ANALYZER_ASSIGNMENT_PROPOSAL_WINDOW_DAYS``) is honored as-is (floored at 1 day), matching
    how base.py/.env.example document the clamp.
    """
    raw = (pref.notification_settings or {}).get("assignment_proposal_window_days")
    try:
        override = int(raw) if raw is not None else None
    except (TypeError, ValueError):
        override = None
    if override is None:
        return max(1, int(default))
    return max(MIN_WINDOW_DAYS, override)


def _mode_and_reachability(repository: Repository) -> dict[str, tuple[str, bool, ReviewerPreference]]:
    """Map normalized reviewer login -> (assignment_acceptance mode, reachable-on-Zulip, preference)."""
    out: dict[str, tuple[str, bool, ReviewerPreference]] = {}
    prefs = ReviewerPreference.objects.filter(repository=repository).select_related("user")
    for pref in prefs:
        login = getattr(pref.user, "github_login", None)
        if not login:
            continue
        reachable = pref.user.zulip_user_id is not None
        out[_normalize_login(login)] = (pref.assignment_acceptance, reachable, pref)
    return out


def propose_assignments_for_repo(
    repository: Repository,
    *,
    run_date,
    now: datetime,
    enabled: bool,
    dry_run: bool,
    window_days: int,
    dedupe_days: int,
    max_age_hours: int,
    max_per_repo: int,
    token_resolver: TokenResolver = resolve_github_app_operation_token,
    assignment_client: GitHubAssignmentClient | None = None,
    sync_enqueuer: SyncEnqueuer = _default_sync_enqueuer,
) -> dict[str, Any]:
    """Propose (or, for auto/fallback reviewers, directly assign) the latest snapshot for a repo.

    Returns a concise per-repo summary for task aggregation / admin debugging. ``dry_run`` is fully
    side-effect-free (no proposal rows, no GitHub writes, no application rows) — it only counts what
    would happen. When neither ``enabled`` nor ``dry_run`` the caller should not invoke this.
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
    mode_by_login = _mode_and_reachability(repository)
    live_by_number = {
        int(pr.number): pr
        for pr in PullRequest.objects.filter(repository=repository, number__in=pr_numbers).only("number", "state", "assignees")
    }
    # PRs that already carry an active proposal (belt-and-suspenders: the builder already excludes
    # them from the snapshot, but a concurrent run could have created one in between).
    prs_with_active_proposal = {
        int(pr_number)
        for pr_number in AssignmentProposal.objects.filter(
            repository=repository,
            pr_number__in=pr_numbers,
            state=AssignmentProposal.STATE_PROPOSED,
        ).values_list("pr_number", flat=True)
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

    def _direct_assign(pr_number: int, login: str, *, route: str) -> None:
        """Run the 046 direct-assign path for an auto/fallback reviewer.

        The per-repo cap bounds GitHub mutations only, so once it is hit we skip further
        direct-assigns (leaving them unrecorded for the next run) but keep iterating so DB-only
        proposals later in the batch are still created.
        """
        nonlocal token, token_attempted, assignment_client
        if (pr_number, _normalize_login(login)) in recently_applied:
            stats["skipped_recently_applied"] += 1
            return
        if dry_run:
            stats["skipped_dry_run"] += 1
            return
        if not enabled:
            stats["skipped_disabled"] += 1
            return
        if max_per_repo > 0 and (stats["assigned_auto"] + stats["assigned_fallback"] + stats["failed"]) >= max_per_repo:
            stats["capped"] = True
            stats["capped_remaining"] += 1
            return
        if not token_attempted:
            token = token_resolver(operation="assign_pr", owner=owner, repo=name)
            token_attempted = True
        if not token:
            stats["skipped_no_token"] += 1
            return
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
            stats["assigned_auto" if route == "auto" else "assigned_fallback"] += 1
        elif outcome == "failed":
            stats["failed"] += 1
        else:  # already_recorded
            stats["skipped_already_recorded"] += 1

    def _create_proposal(pr_number: int, login: str, pref: ReviewerPreference) -> None:
        if dry_run:
            stats["skipped_dry_run"] += 1
            return
        if not enabled:
            stats["skipped_disabled"] += 1
            return
        expires_at = now + timedelta(days=_resolve_window_days(pref, window_days))
        try:
            with transaction.atomic():
                AssignmentProposal.objects.create(
                    repository=repository,
                    pr_number=pr_number,
                    reviewer_login=login,
                    snapshot=snapshot,
                    state=AssignmentProposal.STATE_PROPOSED,
                    expires_at=expires_at,
                )
            stats["proposed"] += 1
        except IntegrityError:
            # A concurrent writer created the one-active-proposal-per-PR row first.
            stats["skipped_already_proposed"] += 1

    for pr_number, login in proposals:
        login_norm = _normalize_login(login)
        live_pr = live_by_number.get(pr_number)

        # --- Re-validate the proposal against live state (snapshot may be ~1 day old). ---
        if login_norm not in eligible_logins:
            stats["skipped_ineligible"] += 1
            continue
        if login_norm in opt_outs.get(pr_number, set()):
            stats["skipped_opted_out"] += 1
            continue
        not_open = live_pr is not None and str(live_pr.state).strip().lower() != "open"
        current_assignees = _current_assignee_logins(live_pr)
        # ANY assignee blocks, mirroring proposal_validity's REASON_PR_ASSIGNED rule ("don't fight
        # the human/self-assignee") — not just assignees who are eligible reviewers. A proposal
        # created for a PR with a non-reviewer assignee would be superseded by the next expiry
        # sweep and re-created (and re-DM'd) by the next propose run, since superseded rows feed
        # neither the cooldown nor the active-proposal exclusion.
        if not_open or current_assignees:
            stats["skipped_already_assigned"] += 1
            continue
        if pr_number in prs_with_active_proposal:
            stats["skipped_already_proposed"] += 1
            continue

        entry = mode_by_login.get(login_norm)
        if entry is None:
            # Login left the reviewer catalog since compute; treat as ineligible (defensive).
            stats["skipped_ineligible"] += 1
            continue
        mode, reachable, pref = entry

        if mode == ReviewerPreference.ACCEPTANCE_AUTO:
            route = "auto"
        elif not reachable:
            route = "fallback"
        else:
            route = "propose"

        if route == "propose":
            _create_proposal(pr_number, login, pref)
        else:
            _direct_assign(pr_number, login, route=route)

    result["status"] = "ok"
    return result


__all__ = ["propose_assignments_for_repo"]
