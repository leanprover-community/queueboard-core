from __future__ import annotations

from django.db import models


class GitHubWebhookDeliveryStatus(models.TextChoices):
    ACCEPTED = "ACCEPTED", "accepted"


class GitHubWebhookDelivery(models.Model):
    """Record processed GitHub webhook deliveries for idempotency/auditing."""

    delivery_id = models.CharField(max_length=255, unique=True)
    event_type = models.CharField(max_length=100)
    action = models.CharField(max_length=100, blank=True, default="")
    repository_owner = models.CharField(max_length=255, blank=True, default="")
    repository_name = models.CharField(max_length=255, blank=True, default="")
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=32, choices=GitHubWebhookDeliveryStatus.choices)
    summary_json = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["event_type", "received_at"], name="ghw_event_recv_idx"),
            models.Index(fields=["repository_owner", "repository_name"], name="ghw_repo_idx"),
        ]
        ordering = ["-received_at", "-id"]

    def __str__(self) -> str:  # pragma: no cover - simple representation
        return f"GitHubWebhookDelivery({self.delivery_id}, {self.event_type})"
