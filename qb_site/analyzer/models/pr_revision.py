from __future__ import annotations

from django.db import models


class PRRevision(models.Model):
    """Head revision window for a PR.

    Represents a contiguous interval [from_ts, to_ts) during which the PR's head SHA
    was `head_sha`. Windows are non-overlapping and ordered by time. The latest window
    has `to_ts = NULL`.
    """

    pull_request = models.ForeignKey("syncer.PullRequest", on_delete=models.CASCADE, related_name="revisions")
    head_sha = models.CharField(max_length=40)
    from_ts = models.DateTimeField()
    to_ts = models.DateTimeField(null=True, blank=True)
    # Optional sequence number for stable ordering/debugging; higher is newer.
    seq = models.IntegerField()

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["pull_request", "-from_ts", "-seq", "id"]
        indexes = [
            models.Index(fields=["pull_request", "from_ts"], name="prrev_pr_from_idx"),
            models.Index(fields=["pull_request", "head_sha"], name="prrev_pr_sha_idx"),
        ]
        constraints = [
            # Windows are keyed by their start timestamp within a PR
            models.UniqueConstraint(fields=("pull_request", "from_ts"), name="prrev_pr_from_unique"),
        ]

    def __str__(self) -> str:  # pragma: no cover - simple representation
        end = self.to_ts.isoformat() if self.to_ts else "..."
        return f"PRRevision({self.pull_request}, {self.head_sha[:7]} {self.from_ts.isoformat()} → {end})"
