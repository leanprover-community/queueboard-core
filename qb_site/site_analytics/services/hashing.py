"""Visitor hashing service for privacy-preserving pageview identity."""

from __future__ import annotations

import hashlib
import time

from django.conf import settings
from django.http import HttpRequest

from site_analytics.models.salt import SiteAnalyticsSalt

# Simple in-process cache so we don't hit the DB on every request.
# Each dyno/worker caches independently; a 60-second TTL means the new salt
# is picked up within a minute of rotation, which is acceptable.
_cached_salt: str = ""
_cache_expires: float = 0.0


def _reset_salt_cache() -> None:
    """Invalidate the in-process salt cache. Intended for use in tests only."""
    global _cached_salt, _cache_expires
    _cached_salt = ""
    _cache_expires = 0.0


def _get_current_salt() -> str:
    global _cached_salt, _cache_expires
    now = time.monotonic()
    if now < _cache_expires and _cached_salt:
        return _cached_salt
    try:
        _cached_salt = SiteAnalyticsSalt.objects.latest("created_at").salt
    except SiteAnalyticsSalt.DoesNotExist:
        # Fall back to the static env-var salt until the first rotation task runs.
        _cached_salt = settings.SITE_ANALYTICS_HASH_SALT
    _cache_expires = now + 60.0
    return _cached_salt


def get_client_ip(request: HttpRequest) -> str:
    """Return the client IP address.

    Prefers the leftmost address in X-Forwarded-For (set by Heroku's routing
    layer and most reverse proxies).  Falls back to REMOTE_ADDR for direct
    connections (local dev, tests).
    """
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "").strip()
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


def compute_visitor_hash(ip: str, user_agent: str) -> str:
    """Return sha256(ip | normalized_ua | salt) as a hex digest.

    Fields are joined with ``|`` to prevent cross-field collisions.
    Cross-month correlation is prevented by the monthly salt rotation: the salt
    is replaced at the start of each month and the old value discarded, so
    hashes from different months are unlinkable even with knowledge of the
    current salt.
    """
    salt = _get_current_salt()
    normalized_ua = user_agent.strip().lower()
    payload = f"{ip}|{normalized_ua}|{salt}"
    return hashlib.sha256(payload.encode()).hexdigest()
