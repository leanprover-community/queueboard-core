"""CI status evaluation against a QueueRules required-context list.

This module provides single-PR CI evaluation that mirrors the logic used by
the batch snapshot builder in ``queueboard_snapshot.py``.  Keeping it here
makes it easy to reuse without pulling in the full snapshot machinery.

Public surface
--------------
- ``ci_status_for_pr(pr, rules, repository)`` — primary entry point.
- ``check_run_ci_status(cr)`` / ``status_context_ci_status(sc)`` — per-row helpers.
- ``context_aggregate_status(check_runs, status_contexts)`` — aggregate one context name.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Q

from analyzer.services.queue_rules import QueueRules
from core.models import Repository
from syncer.models import PullRequest
from syncer.models.ci_enums import CheckRunConclusion, CheckRunStatus, StatusContextState
from syncer.models.commit_check_run import CommitCheckRun
from syncer.models.commit_status_context import CommitStatusContext


def check_run_ci_status(cr: dict) -> str:
    """Map a CommitCheckRun value-dict to a CIStatus string."""
    status = str(cr.get("status") or "").upper()
    conclusion_raw = cr.get("conclusion")
    conclusion = str(conclusion_raw or "").upper() if conclusion_raw is not None else None
    if status in {"IN_PROGRESS", "QUEUED", "PENDING"}:
        return "running"
    if conclusion in {CheckRunConclusion.SUCCESS, CheckRunConclusion.NEUTRAL, CheckRunConclusion.SKIPPED}:
        return "pass"
    if conclusion in {CheckRunConclusion.FAILURE, CheckRunConclusion.CANCELLED, CheckRunConclusion.TIMED_OUT}:
        return "fail"
    if conclusion is None and status == CheckRunStatus.COMPLETED:
        return "fail"
    return "running"


def status_context_ci_status(sc: dict) -> str:
    """Map a CommitStatusContext value-dict to a CIStatus string."""
    state = sc.get("state")
    if state == StatusContextState.SUCCESS:
        return "pass"
    if state == StatusContextState.PENDING:
        return "running"
    if state in (StatusContextState.FAILURE, StatusContextState.ERROR):
        return "fail"
    return "running"


def context_aggregate_status(check_runs: list[dict], status_contexts: list[dict]) -> str:
    """Return the aggregate CI status across check runs and status contexts for one context name.

    Picks the most-recent result for each distinct run name, then returns:
    ``"fail"`` if any are failing, ``"running"`` if any are running,
    ``"pass"`` if all pass, or ``"missing"`` if there are no results at all.
    """
    latest: dict[str, tuple[Any, str]] = {}

    for cr in check_runs:
        name = cr.get("name")
        ts = cr.get("gh_completed_at") or cr.get("gh_started_at")
        if not name or not ts:
            continue
        key = name.lower()
        s = check_run_ci_status(cr)
        current = latest.get(key)
        if current is None or ts > current[0]:
            latest[key] = (ts, s)

    for sc in status_contexts:
        name = sc.get("name")
        ts = sc.get("gh_created_at")
        if not name or not ts:
            continue
        key = name.lower()
        s = status_context_ci_status(sc)
        current = latest.get(key)
        if current is None or ts > current[0]:
            latest[key] = (ts, s)

    if not latest:
        return "missing"
    statuses = [s for _, s in latest.values()]
    if any(s == "fail" for s in statuses):
        return "fail"
    if any(s == "running" for s in statuses):
        return "running"
    return "pass"


def ci_status_for_pr(pr: PullRequest, rules: QueueRules, repository: Repository) -> str:
    """Evaluate PR CI status using the required-context list from the ruleset.

    Queries ``CommitCheckRun`` and ``CommitStatusContext`` rows for the PR's
    head SHA and evaluates each required context in turn.  Returns ``"pass"``
    when no CI gating is configured (no ``require_ci_success`` or no
    ``required_ci_contexts``), and ``"missing"`` when ``head_sha`` is absent.
    """
    if not rules.require_ci_success or not rules.required_ci_contexts:
        return "pass"

    head_sha = pr.head_sha
    if not head_sha:
        return "missing"

    required_contexts = list(rules.required_ci_contexts)

    name_filter = Q()
    for ctx in required_contexts:
        name_filter |= Q(name__icontains=ctx)

    check_runs = list(
        CommitCheckRun.objects.filter(
            repository=repository,
            head_sha=head_sha,
        )
        .filter(name_filter)
        .values("name", "status", "conclusion", "head_sha", "gh_started_at", "gh_completed_at")
    )
    status_contexts = list(
        CommitStatusContext.objects.filter(
            repository=repository,
            head_sha=head_sha,
        )
        .filter(name_filter)
        .values("name", "state", "head_sha", "gh_created_at")
    )

    any_fail = False
    any_running = False
    any_missing = False

    for ctx_name in required_contexts:
        cr_matches = [cr for cr in check_runs if ctx_name in (cr.get("name") or "").lower()]
        sc_matches = [sc for sc in status_contexts if ctx_name in (sc.get("name") or "").lower()]
        status = context_aggregate_status(cr_matches, sc_matches)
        if status == "pass":
            continue
        elif status == "fail":
            any_fail = True
        elif status == "missing":
            any_missing = True
        elif status == "running":
            any_running = True

    if any_fail:
        return "fail"
    if any_missing:
        return "missing"
    if any_running:
        return "running"
    return "pass"
