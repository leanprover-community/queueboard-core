from __future__ import annotations

from django.db import models


class PRQueueWindowBuildState(models.Model):
    """Per-(PR, ruleset) metadata used to track queue-window build freshness."""

    pull_request = models.ForeignKey(
        "syncer.PullRequest",
        on_delete=models.CASCADE,
        related_name="queue_window_build_states",
    )
    rule_set = models.ForeignKey(
        "analyzer.QueueRuleSet",
        on_delete=models.CASCADE,
        related_name="queue_window_build_states",
    )
    revision_version_built = models.PositiveIntegerField(null=True, blank=True)
    windows_built_at = models.DateTimeField(null=True, blank=True)
    last_status = models.CharField(max_length=32, null=True, blank=True)
    last_reason = models.CharField(max_length=128, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "-id"]
        indexes = [
            models.Index(fields=["pull_request"], name="prqwbs_pr_idx"),
            models.Index(fields=["rule_set"], name="prqwbs_ruleset_idx"),
            models.Index(fields=["revision_version_built"], name="prqwbs_rev_built_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=("pull_request", "rule_set"),
                name="prqwbs_pr_ruleset_unique",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - simple representation
        return f"PRQueueWindowBuildState(pr={self.pull_request_id}, ruleset={self.rule_set_id})"
