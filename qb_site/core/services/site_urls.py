"""Resolve the queueboard site's public base URL for building absolute deep-links.

Single source of truth so feature link-builders (the reviewer console, Zulip prefs/registration
links, …) don't each re-implement base-URL resolution: they all resolve ``QUEUEBOARD_BASE_URL``
(``scheme://host`` with no trailing slash) through here.
"""

from __future__ import annotations

from django.conf import settings

__all__ = ["resolve_site_base_url", "build_site_url"]


def resolve_site_base_url() -> str:
    """Return the site base URL (``scheme://host``, no trailing slash), or ``""`` if unconfigured."""
    return str(getattr(settings, "QUEUEBOARD_BASE_URL", "") or "").strip().rstrip("/")


def build_site_url(path: str) -> str:
    """Join ``path`` onto the site base URL. Returns the bare ``path`` when no base is configured.

    ``path`` should start with ``/``; the result is an absolute URL when a base exists (suitable for
    a DM/email) and a site-relative path otherwise (still usable in-app during local dev).
    """
    normalized = path if path.startswith("/") else f"/{path}"
    base = resolve_site_base_url()
    return f"{base}{normalized}" if base else normalized
