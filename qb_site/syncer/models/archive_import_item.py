from __future__ import annotations

from django.db import models

from core.models.base import TimestampedModel
from core.models.repository import Repository


class ArchiveImportItemStatus(models.TextChoices):
    PENDING = "pending", "pending"
    IN_PROGRESS = "in_progress", "in_progress"
    COMPLETED = "completed", "completed"
    FAILED_TRANSIENT = "failed_transient", "failed_transient"
    FAILED_PERMANENT = "failed_permanent", "failed_permanent"
    SKIPPED = "skipped", "skipped"


class ArchiveImportItem(TimestampedModel):
    """One per-PR work item for the archive backfill importer (design doc 043).

    Rows are inserted by the ``bootstrap_archive_worklist`` management command,
    which enumerates per-PR directories under ``data/`` in one of the two
    legacy archive repos (``leanprover-community/queueboard-archive`` and
    ``…/queueboard-archive2``) via the GitHub ``git/trees`` REST API. Each row
    represents one ``data/<N>/pr_info.json`` payload that the per-item
    importer task will fetch from ``raw.githubusercontent.com`` and ingest
    into the live syncer tables.

    Granularity is one row per ``(archive_name, pr_number)`` — a PR present
    in both archives gets two rows. This keeps per-archive history visible
    in the table for debugging which archive contributed a given PR and
    matches the design doc's choice on the open question.
    """

    repository = models.ForeignKey(
        Repository,
        on_delete=models.CASCADE,
        related_name="archive_import_items",
    )
    archive_name = models.CharField(max_length=64)
    pr_number = models.PositiveIntegerField()
    archive_path = models.CharField(max_length=512)
    archive_blob_sha = models.CharField(max_length=64, null=True, blank=True)
    archive_timestamp = models.DateTimeField(null=True, blank=True)

    status = models.CharField(
        max_length=20,
        choices=ArchiveImportItemStatus.choices,
        default=ArchiveImportItemStatus.PENDING,
    )
    attempts = models.IntegerField(default=0)
    last_error = models.TextField(blank=True, default="")
    last_attempted_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["archive_name", "pr_number"],
                name="syncer_archiveitem_archive_pr_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=["status", "last_attempted_at"],
                name="syncer_archiveitem_status_idx",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - simple representation
        return f"{self.archive_name}/data/{self.pr_number} [{self.status}]"
