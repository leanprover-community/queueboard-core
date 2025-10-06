from __future__ import annotations

from django.db import models

from core.models.base import TimestampedModel
from .pull_request import PullRequest
from .label_def import LabelDef


class PRLabel(TimestampedModel):
    """Current label attachment for a PR."""

    pull_request = models.ForeignKey(PullRequest, on_delete=models.CASCADE, related_name="labels")
    label_def = models.ForeignKey(LabelDef, on_delete=models.CASCADE, related_name="attached_to")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["pull_request", "label_def"], name="syncer_prlabel_pr_label_unique"),
        ]
        indexes = [
            models.Index(fields=["pull_request"], name="syncer_prlabel_pr_idx"),
            models.Index(fields=["label_def"], name="syncer_prlabel_label_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - simple representation
        return f"{self.pull_request} ← {self.label_def}"
