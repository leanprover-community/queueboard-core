from __future__ import annotations

from django.db import models


class PRQueueWindow(models.Model):
    """Queue membership window for a PR under a specific rule set.

    Represents a contiguous interval [from_ts, to_ts) during which the PR was
    considered "on the queue" according to a particular QueueRuleSet.
    """

    pull_request = models.ForeignKey("syncer.PullRequest", on_delete=models.CASCADE, related_name="queue_windows")
    rule_set = models.ForeignKey("analyzer.QueueRuleSet", on_delete=models.CASCADE, related_name="queue_windows")

    from_ts = models.DateTimeField()
    to_ts = models.DateTimeField()

    # Monotone counter within (pr, rule_set), grouping consecutive queue segments.
    cycle_index = models.PositiveIntegerField()

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["pull_request", "rule_set", "from_ts", "cycle_index", "id"]
        indexes = [
            models.Index(fields=["pull_request", "rule_set", "from_ts"], name="prqwin_pr_ruleset_from_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=("pull_request", "rule_set", "from_ts"),
                name="prqwin_pr_ruleset_from_unique",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - simple representation
        return f"PRQueueWindow(pr={self.pull_request_id}, ruleset={self.rule_set_id}, {self.from_ts}→{self.to_ts})"
