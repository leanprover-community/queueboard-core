"""Django system checks for site_analytics configuration."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.checks import Error

SALT_MISSING_ID = "site_analytics.E001"


def check_hash_salt_configured(app_configs: Any, **kwargs: Any) -> list[Error]:
    """Require a bootstrap hash salt whenever analytics ingestion is enabled.

    Gated on ``SITE_ANALYTICS_ALLOWED_SITES`` because analytics is opt-in: a
    deployment that never set an allowed site (CI, local dev) accepts no events and
    has nothing to hash, so demanding a salt there would be noise.

    Deliberately reads settings only, never the database.  System checks run before
    migrations have necessarily been applied — ``manage.py migrate`` itself runs them
    — so touching ``SiteAnalyticsSalt`` here would make the first deploy unbootable:
    the check would need the very table the pending migration creates.

    Raising this as an ``Error`` (not a ``Warning``) makes ``manage.py check`` and
    ``migrate`` fail, so a misconfigured deploy stops in the release phase rather
    than silently collecting nothing.  Note that gunicorn does not run system checks
    when loading the WSGI app; the runtime guarantee comes from ``SaltUnavailable``
    in the hashing service, which also covers the salt row vanishing after boot.
    """
    if not settings.SITE_ANALYTICS_ALLOWED_SITES:
        return []
    if settings.SITE_ANALYTICS_HASH_SALT.strip():
        return []
    return [
        Error(
            "SITE_ANALYTICS_HASH_SALT is empty while site analytics ingestion is enabled "
            f"(SITE_ANALYTICS_ALLOWED_SITES={settings.SITE_ANALYTICS_ALLOWED_SITES!r}).",
            hint=(
                "Without a salt, visitor_month_hash would be an unsalted sha256 of IP and "
                "user-agent, which is brute-forceable and therefore not pseudonymous. Set "
                'SITE_ANALYTICS_HASH_SALT to a random secret (e.g. `python -c "import secrets; '
                'print(secrets.token_hex(32))"`). It is only the bootstrap value: once the '
                "site_analytics.rotate_salt task runs, the SiteAnalyticsSalt row takes precedence. "
                "Alternatively clear SITE_ANALYTICS_ALLOWED_SITES to disable ingestion."
            ),
            id=SALT_MISSING_ID,
        )
    ]
