from __future__ import annotations

from django.db import models
from django.db.models import F
from django.db.models.functions import Lower

from core.models.base import TimestampedModel
from core.models.repository import Repository


class LabelDef(TimestampedModel):
    """Label catalog per repository (case-insensitive by name).

    Fields
    - ``repository``: owning repo.
    - ``name``: display name from GitHub; stored as-is but compared case-insensitively.
    - ``color``: 6-digit hex string (no leading '#').

    Constraints
    - Unique per repo on ``lower(name)`` to reflect GitHub's case-insensitive labels while preserving
      the original display casing.
    """

    repository = models.ForeignKey(Repository, on_delete=models.CASCADE, related_name="label_defs")
    name = models.CharField(max_length=100)
    color = models.CharField(max_length=6)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                F("repository"),
                Lower("name"),
                name="syncer_labeldef_repo_lower_name_unique",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - simple representation
        return f"{self.repository}:{self.name}"
