from __future__ import annotations

from django.db import models
from django.utils import timezone

from core.models import Repository, User
from core.models.base import TimestampedModel


class ReviewerAttentionDailyRun(TimestampedModel):
    """Metadata for one reviewer-attention sweep execution."""

    run_date = models.DateField(db_index=True)
    started_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=24, default="started")
    reports_enabled = models.BooleanField(default=False)
    enforcement_enabled = models.BooleanField(default=False)
    delivery_enabled = models.BooleanField(default=False)
    repository = models.ForeignKey(
        Repository,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewer_attention_daily_runs",
    )
    task_id = models.CharField(max_length=255, blank=True, default="")
    summary = models.JSONField(default=dict, blank=True)
    errors = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["-started_at", "-id"]
        indexes = [
            models.Index(fields=["run_date", "status"], name="an_ra_run_date_status_idx"),
        ]


class ReviewerAttentionNotificationRecord(TimestampedModel):
    """Per-item/day notification dedupe and delivery outcome state."""

    STATUS_PENDING = "pending"
    STATUS_SENT = "sent"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_SENT, "Sent"),
        (STATUS_FAILED, "Failed"),
    ]

    CATEGORY_NEW_ASSIGNMENT = "new_assignment"
    CATEGORY_NUDGE = "nudge"
    CATEGORY_AUTO_UNASSIGN = "auto_unassign"
    CATEGORY_CHOICES = [
        (CATEGORY_NEW_ASSIGNMENT, "New assignment"),
        (CATEGORY_NUDGE, "Queue nudge"),
        (CATEGORY_AUTO_UNASSIGN, "Auto-unassign threshold"),
    ]

    run_date = models.DateField()
    repository = models.ForeignKey(
        Repository,
        on_delete=models.CASCADE,
        related_name="reviewer_attention_notification_records",
    )
    reviewer = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="reviewer_attention_notification_records",
    )
    pr_number = models.PositiveIntegerField()
    category = models.CharField(max_length=24, choices=CATEGORY_CHOICES)
    cycle_anchor_at = models.DateTimeField()
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING)
    delivered_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True, default="")
    run = models.ForeignKey(
        ReviewerAttentionDailyRun,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notification_records",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["repository", "reviewer", "pr_number", "category", "cycle_anchor_at"],
                name="an_ra_notify_repo_user_pr_cat_anchor_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["run_date", "status"], name="an_ra_notify_day_status_idx"),
            models.Index(fields=["reviewer", "run_date"], name="an_ra_notify_user_day_idx"),
        ]


class ReviewerAttentionAutoUnassignRecord(TimestampedModel):
    """Per-item/day auto-unassign execution state."""

    STATUS_PENDING = "pending"
    STATUS_UNASSIGNED = "unassigned"
    STATUS_FAILED = "failed"
    STATUS_SKIPPED_NO_TOKEN = "skipped_no_token"
    STATUS_SKIPPED_DISABLED = "skipped_disabled"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_UNASSIGNED, "Unassigned"),
        (STATUS_FAILED, "Failed"),
        (STATUS_SKIPPED_NO_TOKEN, "Skipped (no token)"),
        (STATUS_SKIPPED_DISABLED, "Skipped (disabled)"),
    ]

    run_date = models.DateField()
    repository = models.ForeignKey(
        Repository,
        on_delete=models.CASCADE,
        related_name="reviewer_attention_auto_unassign_records",
    )
    reviewer = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="reviewer_attention_auto_unassign_records",
    )
    pr_number = models.PositiveIntegerField()
    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default=STATUS_PENDING)
    completed_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True, default="")
    run = models.ForeignKey(
        ReviewerAttentionDailyRun,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="auto_unassign_records",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["run_date", "repository", "reviewer", "pr_number"],
                name="an_ra_unassign_day_repo_user_pr_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["run_date", "status"], name="an_ra_unassign_day_status_idx"),
            models.Index(fields=["reviewer", "run_date"], name="an_ra_unassign_user_day_idx"),
        ]
