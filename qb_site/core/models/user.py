from __future__ import annotations

from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower

from .base import TimestampedModel


class User(TimestampedModel):
    """Canonical person entity used across apps.

    Identity
    - ``github_node_id`` holds GitHub's global node id (REST ``node_id`` / GraphQL ``id``). This is
      the preferred stable identifier and supports upserts regardless of API.
    - ``github_login`` stores the current login from GitHub. We enforce case‑insensitive uniqueness
      at the database level so renames can update this field without creating duplicates.

    Zulip
    - ``zulip_user_id`` is the stable Zulip user id (single realm assumed). Prefer this over handles
      for joins. ``zulip_full_name`` is optional display metadata.

    Notes
    - Additional provider identities can be added later. If multiple Zulip realms become necessary,
      we can introduce realm scoping or a separate identity table without changing callers.
    """

    # GitHub identifiers
    github_node_id = models.CharField(max_length=255, unique=True, null=True, blank=True)
    github_login = models.CharField(max_length=255, null=True, blank=True)
    name = models.CharField(max_length=255, null=True, blank=True)
    avatar_url = models.URLField(null=True, blank=True)

    # Optional IANA timezone name for localizing times in UIs and inputs (e.g., "Europe/Berlin").
    # If omitted, fall back to project default timezone settings.
    timezone = models.CharField(max_length=64, null=True, blank=True)

    # Zulip identity (single realm for v1)
    zulip_user_id = models.BigIntegerField(null=True, blank=True, unique=True)
    zulip_full_name = models.CharField(max_length=255, null=True, blank=True)

    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            # Enforce case‑insensitive uniqueness on github_login when present.
            models.UniqueConstraint(
                Lower("github_login"),
                name="core_user_github_login_ci_unique",
                condition=Q(github_login__isnull=False),
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - simple representation
        glogin = self.github_login or "<no-login>"
        return f"{glogin}"
