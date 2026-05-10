from __future__ import annotations

from django.db import models


class PRRevisionBuildState(models.Model):
    """Metadata used to decide whether to append or fully rebuild PRRevision windows."""

    pull_request = models.OneToOneField(
        "syncer.PullRequest",
        on_delete=models.CASCADE,
        related_name="revision_build_state",
    )
    built_through_ts = models.DateTimeField(null=True, blank=True)
    dirty_from_ts = models.DateTimeField(null=True, blank=True)
    builder_version = models.PositiveIntegerField(default=1)
    last_built_at = models.DateTimeField(null=True, blank=True)
    revision_version = models.PositiveIntegerField(default=0)
    ci_checked_revision_version = models.PositiveIntegerField(null=True, blank=True)
    ci_checked_at = models.DateTimeField(null=True, blank=True)

    # Wall-clock high-water mark of "we last wrote a CommitCheckRun or
    # CommitStatusContext row for this PR." Drives the queue-window
    # sweep's CI-staleness predicate (doc 045): if windows_built_at on
    # any (PR, ruleset) row predates latest_ci_synced_at, the windows
    # are out of date with respect to CI evidence and need a rebuild.
    # Advanced only by content-changing CI sub-syncs (no-op syncs leave
    # it unchanged) so the sweep doesn't loop on unchanged CI.
    latest_ci_synced_at = models.DateTimeField(null=True, blank=True, db_index=True)

    # Optional pointer to the known tail window for faster appends; safe to null out.
    tail_revision = models.ForeignKey(
        "analyzer.PRRevision",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="as_tail_for_state",
    )
    tail_from_ts = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "-id"]
        indexes = [
            models.Index(fields=["builder_version"], name="prrbs_version_idx"),
            models.Index(fields=["dirty_from_ts"], name="prrbs_dirty_idx"),
            models.Index(fields=["built_through_ts"], name="prrbs_built_idx"),
            models.Index(fields=["revision_version"], name="prrbs_rev_version_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - simple representation
        return f"PRRevisionBuildState(pr={self.pull_request_id}, built_through={self.built_through_ts}, dirty_from={self.dirty_from_ts})"
