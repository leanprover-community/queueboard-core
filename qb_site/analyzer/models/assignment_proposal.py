from __future__ import annotations

from django.db import models

from core.models import Repository
from core.models.base import TimestampedModel


class AssignmentProposal(TimestampedModel):
    """A proposed reviewer assignment awaiting acceptance (design doc 050).

    In ``confirm`` mode a reviewer is *proposed* for a PR rather than assigned directly: the
    GitHub assignment is executed only once the reviewer accepts on the console. This model is
    the single active/terminal record of that lifecycle and the source for the board /
    ``pr-info`` "proposed" state and for proposal history/analytics.

    Invariants (see design doc 050):
    - At most one active (``proposed``) proposal per PR, enforced by a partial unique
      constraint. "One at a time; advance to the next candidate on decline/expire" then falls
      out of the daily recompute + exclude loop.
    - ``created_at`` (from ``TimestampedModel``) is the proposal time; there is no separate
      ``proposed_at``.
    - Terminal rows (accepted/declined/expired/superseded) are retained history, not live
      state; history is kept indefinitely (no cleanup task).
    """

    STATE_PROPOSED = "proposed"
    STATE_ACCEPTED = "accepted"
    STATE_DECLINED = "declined"
    STATE_EXPIRED = "expired"
    STATE_SUPERSEDED = "superseded"
    STATE_CHOICES = [
        (STATE_PROPOSED, "Proposed"),
        (STATE_ACCEPTED, "Accepted"),
        (STATE_DECLINED, "Declined"),
        (STATE_EXPIRED, "Expired"),
        (STATE_SUPERSEDED, "Superseded"),
    ]

    DECIDED_VIA_CONSOLE = "console"
    DECIDED_VIA_AUTO_EXPIRE = "auto_expire"
    DECIDED_VIA_SYNC_SUPERSEDED = "sync_superseded"
    DECIDED_VIA_CHOICES = [
        (DECIDED_VIA_CONSOLE, "Console"),
        (DECIDED_VIA_AUTO_EXPIRE, "Auto-expire"),
        (DECIDED_VIA_SYNC_SUPERSEDED, "Sync superseded"),
    ]

    repository = models.ForeignKey(Repository, on_delete=models.CASCADE, related_name="assignment_proposals")
    pr_number = models.PositiveIntegerField()
    reviewer_login = models.CharField(max_length=255)
    snapshot = models.ForeignKey(
        "analyzer.ReviewerAssignmentSnapshot",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="proposals",
    )
    state = models.CharField(max_length=16, choices=STATE_CHOICES, default=STATE_PROPOSED)
    # The acceptance deadline; a still-`proposed` row past this is expired by the sweep.
    expires_at = models.DateTimeField()
    decided_at = models.DateTimeField(null=True, blank=True)
    notified_at = models.DateTimeField(null=True, blank=True)
    decided_via = models.CharField(max_length=32, choices=DECIDED_VIA_CHOICES, blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["repository", "pr_number"],
                condition=models.Q(state="proposed"),  # AssignmentProposal.STATE_PROPOSED
                name="an_ap_one_active_proposal_per_pr",
            ),
        ]
        indexes = [
            models.Index(fields=["repository", "reviewer_login", "state", "decided_at"], name="an_ap_reviewer_state_idx"),
            models.Index(fields=["repository", "pr_number", "state"], name="an_ap_pr_state_idx"),
        ]
        ordering = ["repository", "pr_number", "-id"]

    def __str__(self) -> str:  # pragma: no cover - simple representation
        return (
            f"AssignmentProposal(repo={self.repository}, pr={self.pr_number}, reviewer={self.reviewer_login}, state={self.state})"
        )
