from __future__ import annotations

from django.db import models


class QueueWindowEventType(models.TextChoices):
    REQUIRED_LABEL_ADDED = "REQUIRED_LABEL_ADDED", "required_label_added"
    REQUIRED_LABEL_REMOVED = "REQUIRED_LABEL_REMOVED", "required_label_removed"
    FORBIDDEN_LABEL_ADDED = "FORBIDDEN_LABEL_ADDED", "forbidden_label_added"
    FORBIDDEN_LABEL_REMOVED = "FORBIDDEN_LABEL_REMOVED", "forbidden_label_removed"
    CI_PASSED = "CI_PASSED", "ci_passed"
    CI_FAILED = "CI_FAILED", "ci_failed"
    PR_OPENED = "PR_OPENED", "pr_opened"
    DRAFT_CONVERTED = "DRAFT_CONVERTED", "draft_converted"
    CONVERTED_TO_DRAFT = "CONVERTED_TO_DRAFT", "converted_to_draft"
    PR_CLOSED = "PR_CLOSED", "pr_closed"
    HEAD_PUSHED = "HEAD_PUSHED", "head_pushed"
    INITIAL_STATE = "INITIAL_STATE", "initial_state"
    RULESET_EFFECTIVE = "RULESET_EFFECTIVE", "ruleset_effective"
    UNKNOWN = "UNKNOWN", "unknown"


class PRQueueWindow(models.Model):
    """Queue membership window for a PR under a specific rule set.

    Represents a contiguous interval [from_ts, to_ts) during which the PR was
    considered "on the queue" according to a particular QueueRuleSet.
    """

    pull_request = models.ForeignKey("syncer.PullRequest", on_delete=models.CASCADE, related_name="queue_windows")
    rule_set = models.ForeignKey("analyzer.QueueRuleSet", on_delete=models.CASCADE, related_name="queue_windows")

    from_ts = models.DateTimeField()
    to_ts = models.DateTimeField(null=True, blank=True)

    # Monotone counter within (pr, rule_set), grouping consecutive queue segments.
    cycle_index = models.PositiveIntegerField()
    duration_seconds_closed = models.BigIntegerField(default=0)
    cumulative_seconds_closed = models.BigIntegerField(default=0)
    window_count = models.PositiveIntegerField(default=0)
    first_on_queue_ts = models.DateTimeField(null=True, blank=True)

    # Event attribution: what caused this window to open.
    opened_by_event_type = models.CharField(max_length=32, null=True, blank=True, choices=QueueWindowEventType.choices)
    opened_by_timeline_event = models.ForeignKey(
        "syncer.PRTimelineEvent", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    opened_by_check_run = models.ForeignKey(
        "syncer.CommitCheckRun", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    opened_by_status_context = models.ForeignKey(
        "syncer.CommitStatusContext", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    opened_at_head_sha = models.CharField(max_length=40, null=True, blank=True)

    # Event attribution: what caused this window to close (null while still open).
    closed_by_event_type = models.CharField(max_length=32, null=True, blank=True, choices=QueueWindowEventType.choices)
    closed_by_timeline_event = models.ForeignKey(
        "syncer.PRTimelineEvent", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    closed_by_check_run = models.ForeignKey(
        "syncer.CommitCheckRun", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    closed_by_status_context = models.ForeignKey(
        "syncer.CommitStatusContext", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    closed_at_head_sha = models.CharField(max_length=40, null=True, blank=True)

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
