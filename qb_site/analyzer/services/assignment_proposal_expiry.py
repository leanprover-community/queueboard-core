"""Expire and reconcile pending ``AssignmentProposal`` rows (design doc 050).

Essential maintenance for the acceptance gate, intentionally *not* gated by the master switch so
that flipping the gate off lets existing proposals drain. For each still-``proposed`` row it asks
the single ``proposal_validity`` authority whether the proposal is still live, and retires it
otherwise:

- past its acceptance window        -> ``expired`` (seeds the soft re-propose cooldown),
- PR closed/merged or a human/self-assignee landed, or (``invalidate`` policy) the PR left the
  review queue -> ``superseded``.

The sweep performs no GitHub writes — it only transitions DB state, so it is cheap and safe to run
frequently. Off-queue invalidation is applied only when a *fresh* queue snapshot knows about the PR,
so a missing/stale snapshot can never mass-supersede.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from django.db import DatabaseError

from analyzer.models import AssignmentProposal, QueueSnapshot
from analyzer.services.assignment_proposal_validity import ProposalValidity, proposal_validity, resolve_on_queue_exit_policy
from analyzer.services.queue_rules import default_rule_set_for_repo
from analyzer.services.reviewer_assignment_engine import _normalize_login
from core.models import Repository
from syncer.models import PullRequest

log = logging.getLogger(__name__)

# A queue snapshot older than this is not trusted for off-queue determination (mirrors pr_info).
SNAPSHOT_FRESH_SECONDS = 7200


def _empty_stats() -> dict[str, Any]:
    return {
        "active": 0,
        "expired": 0,
        "superseded": 0,
        "still_live": 0,
        "errored": 0,
    }


def _queue_membership(repository: Repository, *, now: datetime) -> tuple[set[int], set[int], bool]:
    """Return ``(queue_pr_numbers, known_pr_numbers, fresh)`` from the latest default snapshot.

    ``known_pr_numbers`` are the PRs the snapshot actually described; only for those (and only when
    ``fresh``) can we assert on-queue membership. Everything else yields ``on_queue=None`` upstream.
    """
    rule_set = default_rule_set_for_repo(repository)
    cache_key = str(rule_set.id) if rule_set else "default"
    snapshot = QueueSnapshot.objects.filter(repository=repository, cache_key=cache_key).order_by("-generated_at").first()
    if snapshot is None or not snapshot.payload:
        return set(), set(), False
    generated_at = snapshot.generated_at
    fresh = generated_at is not None and (now - generated_at).total_seconds() <= SNAPSHOT_FRESH_SECONDS
    payload = snapshot.payload
    queue_prs = {int(n) for n in (payload.get("lists", {}).get("dashboards", {}).get("Queue", []) or [])}
    known_prs = {int(n) for n in (payload.get("prs", {}) or {}).keys()}
    return queue_prs, known_prs, fresh


def expire_and_reconcile_proposals_for_repo(repository: Repository, *, now: datetime) -> dict[str, Any]:
    """Retire every pending proposal for ``repository`` that is no longer live. Returns a summary."""
    result: dict[str, Any] = {
        "repo": f"{repository.owner}/{repository.name}",
        "repo_id": int(repository.id),
        "stats": _empty_stats(),
    }
    stats = result["stats"]

    proposals = list(AssignmentProposal.objects.filter(repository=repository, state=AssignmentProposal.STATE_PROPOSED))
    stats["active"] = len(proposals)
    if not proposals:
        result["status"] = "ok"
        return result

    pr_numbers = {int(p.pr_number) for p in proposals}
    live_by_number = {
        int(pr.number): pr
        for pr in PullRequest.objects.filter(repository=repository, number__in=pr_numbers).only("number", "state", "assignees")
    }
    queue_prs, known_prs, snapshot_fresh = _queue_membership(repository, now=now)
    policy = resolve_on_queue_exit_policy()

    for proposal in proposals:
        try:
            pr_number = int(proposal.pr_number)
            live_pr = live_by_number.get(pr_number)
            pr_state = None if live_pr is None else str(live_pr.state)
            current_assignees = set() if live_pr is None else {_normalize_login(str(a)) for a in (live_pr.assignees or []) if a}
            if snapshot_fresh and pr_number in known_prs:
                on_queue: bool | None = pr_number in queue_prs
            else:
                on_queue = None

            validity: ProposalValidity = proposal_validity(
                proposal,
                now=now,
                pr_state=pr_state,
                current_assignees=current_assignees,
                on_queue=on_queue,
                on_queue_exit=policy,
            )
            if validity.is_live or validity.terminal_state is None:
                stats["still_live"] += 1
                continue

            # Conditional update keeps concurrent sweeps idempotent: only the writer that still sees
            # PROPOSED transitions the row (auto_now updated_at does not fire on .update()).
            updated = AssignmentProposal.objects.filter(id=proposal.id, state=AssignmentProposal.STATE_PROPOSED).update(
                state=validity.terminal_state,
                decided_at=now,
                decided_via=validity.decided_via or "",
                updated_at=now,
            )
            if not updated:
                stats["still_live"] += 1
                continue
            if validity.terminal_state == AssignmentProposal.STATE_EXPIRED:
                stats["expired"] += 1
            else:
                stats["superseded"] += 1
        except DatabaseError:
            stats["errored"] += 1
            log.exception(
                "assignment_proposal_expiry: failed to reconcile proposal id=%s repo=%s/%s pr=%s",
                proposal.id,
                repository.owner,
                repository.name,
                proposal.pr_number,
            )

    result["status"] = "ok"
    return result


__all__ = ["expire_and_reconcile_proposals_for_repo", "SNAPSHOT_FRESH_SECONDS"]
