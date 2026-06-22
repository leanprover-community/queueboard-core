from __future__ import annotations

from django.db import models

from core.models import Repository
from core.models.base import TimestampedModel


class ReviewerAssignmentApplication(TimestampedModel):
    """Per-run record of applying a proposed reviewer assignment to GitHub.

    The reviewer assignment *producer* (``analyzer.refresh_reviewer_assignments``)
    stores immutable, advisory ``ReviewerAssignmentSnapshot`` payloads. This model
    records the *application* of those proposals to GitHub by
    ``analyzer.apply_reviewer_assignments``: which ``(repository, pr_number,
    reviewer_login)`` pairs were assigned, skipped (and why), or failed. It backs
    both idempotency (a recently-applied pair is not re-posted while sync catches up)
    and operator audit.
    """

    STATUS_PENDING = "pending"
    STATUS_APPLIED = "applied"
    STATUS_FAILED = "failed"
    STATUS_SKIPPED_ALREADY_ASSIGNED = "skipped_already_assigned"
    STATUS_SKIPPED_OPTED_OUT = "skipped_opted_out"
    STATUS_SKIPPED_INELIGIBLE = "skipped_ineligible"
    STATUS_SKIPPED_RECENTLY_APPLIED = "skipped_recently_applied"
    STATUS_SKIPPED_NO_TOKEN = "skipped_no_token"
    STATUS_SKIPPED_DISABLED = "skipped_disabled"
    STATUS_SKIPPED_DRY_RUN = "skipped_dry_run"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_APPLIED, "Applied"),
        (STATUS_FAILED, "Failed"),
        (STATUS_SKIPPED_ALREADY_ASSIGNED, "Skipped (already assigned)"),
        (STATUS_SKIPPED_OPTED_OUT, "Skipped (opted out)"),
        (STATUS_SKIPPED_INELIGIBLE, "Skipped (reviewer ineligible)"),
        (STATUS_SKIPPED_RECENTLY_APPLIED, "Skipped (recently applied)"),
        (STATUS_SKIPPED_NO_TOKEN, "Skipped (no token)"),
        (STATUS_SKIPPED_DISABLED, "Skipped (disabled)"),
        (STATUS_SKIPPED_DRY_RUN, "Skipped (dry run)"),
    ]

    run_date = models.DateField(db_index=True)
    repository = models.ForeignKey(
        Repository,
        on_delete=models.CASCADE,
        related_name="reviewer_assignment_applications",
    )
    pr_number = models.PositiveIntegerField()
    reviewer_login = models.CharField(max_length=255)
    snapshot = models.ForeignKey(
        "analyzer.ReviewerAssignmentSnapshot",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="applications",
    )
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_PENDING)
    applied_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["run_date", "repository", "pr_number", "reviewer_login"],
                name="an_raa_day_repo_pr_reviewer_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=["repository", "pr_number", "reviewer_login", "status"],
                name="an_raa_repo_pr_rev_status_idx",
            ),
            models.Index(fields=["run_date", "status"], name="an_raa_day_status_idx"),
        ]
        ordering = ["-run_date", "repository", "pr_number", "id"]

    def __str__(self) -> str:  # pragma: no cover - simple representation
        return f"ReviewerAssignmentApplication(repo={self.repository}, pr={self.pr_number}, reviewer={self.reviewer_login})"
