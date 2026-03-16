from __future__ import annotations

from django.db import models


class CheckRunStatus(models.TextChoices):
    QUEUED = "QUEUED", "queued"
    IN_PROGRESS = "IN_PROGRESS", "in_progress"
    COMPLETED = "COMPLETED", "completed"


class CheckRunConclusion(models.TextChoices):
    SUCCESS = "SUCCESS", "success"
    FAILURE = "FAILURE", "failure"
    CANCELLED = "CANCELLED", "cancelled"
    NEUTRAL = "NEUTRAL", "neutral"
    SKIPPED = "SKIPPED", "skipped"
    TIMED_OUT = "TIMED_OUT", "timed_out"
    ACTION_REQUIRED = "ACTION_REQUIRED", "action_required"


class StatusContextState(models.TextChoices):
    SUCCESS = "SUCCESS", "success"
    FAILURE = "FAILURE", "failure"
    ERROR = "ERROR", "error"
    PENDING = "PENDING", "pending"
