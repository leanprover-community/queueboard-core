from __future__ import annotations

from django.db import models

from core.models.base import TimestampedModel
from core.models.repository import Repository


def resolve_ci_gating_mode(*, require_ci_success: bool, ci_gating_mode: str | None) -> str | None:
    """Return the effective CI gating mode for transitional ruleset rows.

    During migration, ``require_ci_success=False`` keeps CI gating disabled,
    regardless of any mode value.
    """
    if not require_ci_success:
        return None
    if ci_gating_mode == QueueRuleSet.CIGatingMode.NO_REQUIRED_FAILURES:
        return QueueRuleSet.CIGatingMode.NO_REQUIRED_FAILURES
    return QueueRuleSet.CIGatingMode.ALL_REQUIRED_SUCCESS


class QueueRuleSet(TimestampedModel):
    """Per-repository queue rules with simple label and CI gating.

    Semantics
    - A PR is considered eligible for the queue when:
      - ``require_open`` is True and the PR is open at time T.
      - ``require_not_draft`` is True and the PR is not draft at time T.
      - Every label in ``required_label_names`` (if any) is present on the PR at time T.
      - No label from ``forbidden_label_names`` is present on the PR at time T.
      - Effective CI gating mode (derived from ``require_ci_success`` and ``ci_gating_mode``)
        marks CI as eligible at time T.
    - Label names are compared case-insensitively; rules are stored as plain strings
      to avoid hard-coupling to the Syncer label catalog.
    - Effective bounds:
      - If ``effective_from`` is set, the ruleset is intended to apply only to PR
        history at or after that timestamp.
      - If ``effective_to`` is set, the ruleset is intended to apply only to PR
        history strictly before that timestamp (i.e., [effective_from, effective_to)).
    """

    class CIGatingMode(models.TextChoices):
        ALL_REQUIRED_SUCCESS = "all_required_success", "All required contexts must succeed"
        NO_REQUIRED_FAILURES = "no_required_failures", "Only required-context failures block queue entry"

    repository = models.ForeignKey(Repository, on_delete=models.CASCADE, related_name="queue_rule_sets")
    version = models.PositiveIntegerField(default=1)
    description = models.TextField(blank=True)

    # Optional activation window for this ruleset; used to steer which ruleset is
    # applied for a given PR/time when multiple versions exist.
    effective_from = models.DateTimeField(null=True, blank=True)
    effective_to = models.DateTimeField(null=True, blank=True)

    # Whether this ruleset should be considered when building snapshots/windows.
    is_active = models.BooleanField(default=True)

    # Designates this as the canonical default ruleset for the repository.
    # At most one ruleset per repository may have is_default=True (enforced by
    # a partial unique constraint). When no default is designated the system
    # falls back to the highest-version active ruleset.
    is_default = models.BooleanField(default=False)

    require_open = models.BooleanField(default=True)
    require_not_draft = models.BooleanField(default=True)
    require_ci_success = models.BooleanField(default=False)
    ci_gating_mode = models.CharField(
        max_length=64,
        choices=CIGatingMode.choices,
        default=CIGatingMode.ALL_REQUIRED_SUCCESS,
    )

    required_label_names = models.JSONField(default=list, blank=True)
    forbidden_label_names = models.JSONField(default=list, blank=True)
    # Optional: specific CI contexts (job names) that must succeed for this rule set.
    # These are interpreted as case-insensitive substrings of CI context names.
    required_ci_contexts = models.JSONField(default=list, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["repository", "version"],
                name="analyzer_queueruleset_repo_version_unique",
            ),
            models.UniqueConstraint(
                fields=["repository"],
                condition=models.Q(is_default=True),
                name="analyzer_queueruleset_repo_single_default",
            ),
        ]
        ordering = ["repository", "-version", "id"]

    def effective_ci_gating_mode(self) -> str | None:
        return resolve_ci_gating_mode(require_ci_success=self.require_ci_success, ci_gating_mode=self.ci_gating_mode)

    def __str__(self) -> str:  # pragma: no cover - simple representation
        return f"QueueRuleSet(repo={self.repository}, v={self.version})"
