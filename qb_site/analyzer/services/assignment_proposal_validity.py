"""The single authority on whether a pending ``AssignmentProposal`` is still live.

Design doc 050 deliberately centralizes "is this pending proposal still actionable, and if not,
why" in one predicate rather than scattering "still valid?" checks. Three call sites consult it so
they can never drift:

- the expiry/reconcile sweep (``analyzer.expire_assignment_proposals``),
- sync-time reconciliation (a human/self-assignee landing on a proposed PR),
- the console accept-time re-validation (Chunk 6).

The on-queue-exit behavior is selectable via ``ANALYZER_ASSIGNMENT_PROPOSAL_ON_QUEUE_EXIT``
(``invalidate`` default / ``retain``), read here so a future flip needs no rework elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from django.conf import settings

from analyzer.models import AssignmentProposal, QueueSnapshot
from analyzer.services.queue_rules import default_rule_set_for_repo
from analyzer.services.reviewer_assignment_engine import _normalize_login
from core.models import Repository
from syncer.models import PullRequest

ON_QUEUE_EXIT_INVALIDATE = "invalidate"
ON_QUEUE_EXIT_RETAIN = "retain"

# A queue snapshot older than this is not trusted for off-queue determination (mirrors pr_info).
SNAPSHOT_FRESH_SECONDS = 7200

# reason codes
REASON_LIVE = "live"
REASON_ALREADY_TERMINAL = "already_terminal"
REASON_EXPIRED = "expired"
REASON_PR_ASSIGNED = "pr_assigned"
REASON_PR_CLOSED = "pr_closed"
REASON_PR_OFF_QUEUE = "pr_off_queue"


@dataclass(frozen=True)
class ProposalValidity:
    """Verdict for one pending proposal.

    ``is_live`` is the headline answer. When it is ``False`` and the proposal is not *already*
    terminal, ``terminal_state``/``decided_via`` say how to retire it; ``reason`` is a stable code
    for logging and for the console's "no longer available, here's why" rendering.
    """

    is_live: bool
    reason: str
    terminal_state: str | None = None
    decided_via: str | None = None


def resolve_on_queue_exit_policy() -> str:
    """Return the configured on-queue-exit policy, defaulting to ``invalidate``."""
    value = str(getattr(settings, "ANALYZER_ASSIGNMENT_PROPOSAL_ON_QUEUE_EXIT", ON_QUEUE_EXIT_INVALIDATE)).strip().lower()
    return value if value in (ON_QUEUE_EXIT_INVALIDATE, ON_QUEUE_EXIT_RETAIN) else ON_QUEUE_EXIT_INVALIDATE


def queue_membership(repository: Repository, *, now: datetime) -> tuple[set[int], set[int], bool]:
    """Return ``(queue_pr_numbers, known_pr_numbers, fresh)`` from the latest default snapshot.

    ``known_pr_numbers`` are the PRs the snapshot actually described; only for those (and only when
    ``fresh``) can a caller assert on-queue membership. Everything else must yield ``on_queue=None``
    so a stale/missing snapshot never mass-invalidates — ``live_proposal_validity`` applies that
    rule. This is the one shared assembly of queue-membership facts for validity consumers (the
    expiry sweep and the console); do not reimplement it per call site.
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


def live_proposal_validity(
    proposal: AssignmentProposal,
    *,
    now: datetime,
    live_pr: PullRequest | None,
    membership: tuple[set[int], set[int], bool],
    on_queue_exit: str | None = None,
) -> ProposalValidity:
    """Assemble the durable facts for one proposal and run ``proposal_validity`` on them.

    ``membership`` is the repo-level ``queue_membership(...)`` result, computed once per repo so
    batch callers (the expiry sweep) don't refetch the snapshot per proposal. ``live_pr`` is the
    proposal's PR row (``.only("number", "state", "assignees")`` suffices), or ``None`` if unknown.
    """
    queue_prs, known_prs, fresh = membership
    pr_number = int(proposal.pr_number)
    pr_state = None if live_pr is None else str(live_pr.state)
    current_assignees = set() if live_pr is None else {_normalize_login(str(a)) for a in (live_pr.assignees or []) if a}
    on_queue = (pr_number in queue_prs) if (fresh and pr_number in known_prs) else None
    return proposal_validity(
        proposal,
        now=now,
        pr_state=pr_state,
        current_assignees=current_assignees,
        on_queue=on_queue,
        on_queue_exit=on_queue_exit,
    )


def proposal_validity(
    proposal: AssignmentProposal,
    *,
    now: datetime,
    pr_state: str | None,
    current_assignees: set[str] | None = None,
    on_queue: bool | None = None,
    on_queue_exit: str | None = None,
) -> ProposalValidity:
    """Decide whether ``proposal`` is still a live pending proposal.

    Inputs are durable facts, mirroring the state model's "reconstructable from GitHub assignees +
    the single active proposal":

    - ``pr_state``: live PR state (``"open"`` / ``"closed"`` / ``"merged"`` / ``None`` if unknown).
    - ``current_assignees``: normalized GitHub assignee logins currently on the PR.
    - ``on_queue``: review-queue membership, or ``None`` when the caller could not determine it
      reliably (e.g. no fresh queue snapshot) — in that case the off-queue check is skipped rather
      than guessed, so a stale/missing snapshot never mass-invalidates.

    Precedence (structural invalidation before time before policy):

    1. Not ``proposed`` -> already terminal (nothing to do).
    2. A GitHub assignee has landed -> superseded (don't fight the human/self-assignee).
    3. PR closed/merged -> superseded (a closed PR can't be usefully reviewed, regardless of policy).
    4. Past ``expires_at`` -> expired (a timeout is an expiry even under ``retain``; ``retain`` is
       about queue-exit, not the acceptance clock). Expiry is what seeds the soft re-propose cooldown.
    5. Open but off-queue, under the ``invalidate`` policy -> superseded.
    6. Otherwise -> live.
    """
    if proposal.state != AssignmentProposal.STATE_PROPOSED:
        return ProposalValidity(is_live=False, reason=REASON_ALREADY_TERMINAL)

    policy = (on_queue_exit or resolve_on_queue_exit_policy()).strip().lower()
    assignees = current_assignees or set()

    if assignees:
        return ProposalValidity(
            is_live=False,
            reason=REASON_PR_ASSIGNED,
            terminal_state=AssignmentProposal.STATE_SUPERSEDED,
            decided_via=AssignmentProposal.DECIDED_VIA_SYNC_SUPERSEDED,
        )

    if pr_state is not None and pr_state.strip().lower() != "open":
        return ProposalValidity(
            is_live=False,
            reason=REASON_PR_CLOSED,
            terminal_state=AssignmentProposal.STATE_SUPERSEDED,
            decided_via=AssignmentProposal.DECIDED_VIA_SYNC_SUPERSEDED,
        )

    if proposal.expires_at is not None and now >= proposal.expires_at:
        return ProposalValidity(
            is_live=False,
            reason=REASON_EXPIRED,
            terminal_state=AssignmentProposal.STATE_EXPIRED,
            decided_via=AssignmentProposal.DECIDED_VIA_AUTO_EXPIRE,
        )

    if on_queue is False and policy == ON_QUEUE_EXIT_INVALIDATE:
        return ProposalValidity(
            is_live=False,
            reason=REASON_PR_OFF_QUEUE,
            terminal_state=AssignmentProposal.STATE_SUPERSEDED,
            decided_via=AssignmentProposal.DECIDED_VIA_SYNC_SUPERSEDED,
        )

    return ProposalValidity(is_live=True, reason=REASON_LIVE)


__all__ = [
    "ON_QUEUE_EXIT_INVALIDATE",
    "ON_QUEUE_EXIT_RETAIN",
    "SNAPSHOT_FRESH_SECONDS",
    "ProposalValidity",
    "live_proposal_validity",
    "proposal_validity",
    "queue_membership",
    "resolve_on_queue_exit_policy",
    "REASON_LIVE",
    "REASON_ALREADY_TERMINAL",
    "REASON_EXPIRED",
    "REASON_PR_ASSIGNED",
    "REASON_PR_CLOSED",
    "REASON_PR_OFF_QUEUE",
]
