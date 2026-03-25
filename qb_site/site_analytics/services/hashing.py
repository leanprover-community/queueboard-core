"""Visitor hashing service for privacy-preserving pageview identity."""

from __future__ import annotations

import hashlib

from django.conf import settings
from django.http import HttpRequest


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


def compute_visitor_month_hash(ip: str, user_agent: str, month_key: str) -> str:
    """Return sha256(ip | normalized_ua | month_key | salt) as a hex digest.

    Fields are joined with ``|`` to prevent cross-field collisions.
    ``month_key`` must be a UTC ``YYYY-MM`` string so cross-month correlation
    is impossible by construction.
    """
    salt = settings.SITE_ANALYTICS_HASH_SALT
    normalized_ua = user_agent.strip().lower()
    payload = f"{ip}|{normalized_ua}|{month_key}|{salt}"
    return hashlib.sha256(payload.encode()).hexdigest()
