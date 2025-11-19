from __future__ import annotations

from django.db import models

from core.models.base import TimestampedModel
from core.models.repository import Repository


class QueueRuleSet(TimestampedModel):
    """Per-repository queue rules with simple label and CI gating.

    Semantics (v1)
    - A PR is considered eligible for the queue when:
      - ``require_open`` is True and the PR is open at time T.
      - ``require_not_draft`` is True and the PR is not draft at time T.
      - Every label in ``required_label_names`` (if any) is present on the PR at time T.
      - No label from ``forbidden_label_names`` is present on the PR at time T.
      - ``require_ci_success`` is False, or CI is known to be successful at time T
        (CI integration is a later addition; for now this flag is expected to remain False).
    - Label names are compared case-insensitively; rules are stored as plain strings
      to avoid hard-coupling to the Syncer label catalog.
    """

    repository = models.ForeignKey(Repository, on_delete=models.CASCADE, related_name="queue_rule_sets")
    version = models.PositiveIntegerField(default=1)
    description = models.TextField(blank=True)

    require_open = models.BooleanField(default=True)
    require_not_draft = models.BooleanField(default=True)
    require_ci_success = models.BooleanField(default=False)

    required_label_names = models.JSONField(default=list, blank=True)
    forbidden_label_names = models.JSONField(default=list, blank=True)
    # Optional: specific CI contexts (job names) that must succeed for this rule set.
    # These should match the names persisted in CheckRun.name / StatusContext.name.
    required_ci_contexts = models.JSONField(default=list, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["repository", "version"],
                name="analyzer_queueruleset_repo_version_unique",
            ),
        ]
        ordering = ["repository", "-version", "id"]

    def __str__(self) -> str:  # pragma: no cover - simple representation
        return f"QueueRuleSet(repo={self.repository}, v={self.version})"
