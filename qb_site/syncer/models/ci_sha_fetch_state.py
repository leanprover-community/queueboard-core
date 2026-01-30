from __future__ import annotations

from django.db import models

from core.models import Repository
from core.models.base import TimestampedModel


class CIShaFetchState(TimestampedModel):
    """Track CI-by-SHA fetch attempts per repository+sha."""

    repository = models.ForeignKey(Repository, on_delete=models.CASCADE)
    sha = models.CharField(max_length=64, db_index=True)
    last_attempted_at = models.DateTimeField()
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_result = models.CharField(max_length=32)
    attempts = models.IntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["repository", "sha"], name="syncer_cishafetchstate_repo_sha_uniq"),
        ]
