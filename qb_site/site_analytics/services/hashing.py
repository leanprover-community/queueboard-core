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


class SaltUnavailable(RuntimeError):
    """Raised when no visitor-hash salt is configured.

    Callers must drop the event rather than hash without one: sha256(ip | ua) with
    no secret is brute-forceable across the whole IPv4 space, so an unsalted hash
    is a recoverable identifier rather than a pseudonymous one.  Collecting nothing
    is the correct failure mode for a privacy-preserving pipeline.
    """


def _get_current_salt() -> str:
    """Return the active hash salt, or "" when none is configured.

    The result is cached for 60s *including* the empty one: a deployment with no
    salt would otherwise re-query on every request, and 60s is short enough to pick
    up the first ``rotate_salt`` write.
    """
    global _cached_salt, _cache_expires
    now = time.monotonic()
    if now < _cache_expires:
        return _cached_salt
    try:
        _cached_salt = SiteAnalyticsSalt.objects.latest("created_at").salt
    except SiteAnalyticsSalt.DoesNotExist:
        # Fall back to the static env-var salt until the first rotation task runs.
        _cached_salt = settings.SITE_ANALYTICS_HASH_SALT
    _cache_expires = now + 60.0
    return _cached_salt


def get_client_ip(request: HttpRequest) -> str:
    """Return the client IP address, trusting only the proxy hops we run behind.

    X-Forwarded-For is client-controlled: a caller may send any value it likes, and
    each proxy *appends* the address it received the connection from.  Heroku's router
    appends the connecting IP, so with one proxy hop the rightmost entry is the only
    one we can trust; the leftmost is whatever the client chose to claim.  Reading the
    leftmost entry would let a visitor mint a fresh ``visitor_month_hash`` per request
    and inflate unique-visitor counts at will.

    ``SITE_ANALYTICS_TRUSTED_PROXY_COUNT`` is the number of proxies in front of this
    app; we take that many entries from the right.  Set it to 0 when the app is exposed
    directly (no proxy), in which case X-Forwarded-For is ignored entirely and only
    REMOTE_ADDR is used.
    """
    num_proxies = settings.SITE_ANALYTICS_TRUSTED_PROXY_COUNT
    if num_proxies > 0:
        xff = request.META.get("HTTP_X_FORWARDED_FOR", "").strip()
        addrs = [part.strip() for part in xff.split(",") if part.strip()]
        if addrs:
            # Clamp to the chain length so a shorter-than-expected chain still yields
            # the leftmost real entry rather than raising IndexError.
            return addrs[-min(num_proxies, len(addrs))]
    return request.META.get("REMOTE_ADDR", "")


def compute_visitor_hash(ip: str, user_agent: str) -> str:
    """Return sha256(ip | normalized_ua | salt) as a hex digest.

    Fields are joined with ``|`` to prevent cross-field collisions.
    Cross-month correlation is prevented by the monthly salt rotation: the salt
    is replaced at the start of each month and the old value discarded, so
    hashes from different months are unlinkable even with knowledge of the
    current salt.

    Raises ``SaltUnavailable`` when no salt is configured; callers must drop the
    event rather than store an unsalted hash.
    """
    salt = _get_current_salt()
    if not salt:
        raise SaltUnavailable(
            "no SiteAnalyticsSalt row exists and SITE_ANALYTICS_HASH_SALT is empty; refusing to compute an unsalted visitor hash"
        )
    normalized_ua = user_agent.strip().lower()
    payload = f"{ip}|{normalized_ua}|{salt}"
    return hashlib.sha256(payload.encode()).hexdigest()
