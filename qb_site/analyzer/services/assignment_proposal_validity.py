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

from analyzer.models import AssignmentProposal

ON_QUEUE_EXIT_INVALIDATE = "invalidate"
ON_QUEUE_EXIT_RETAIN = "retain"

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
    "ProposalValidity",
    "proposal_validity",
    "resolve_on_queue_exit_policy",
    "REASON_LIVE",
    "REASON_ALREADY_TERMINAL",
    "REASON_EXPIRED",
    "REASON_PR_ASSIGNED",
    "REASON_PR_CLOSED",
    "REASON_PR_OFF_QUEUE",
]
