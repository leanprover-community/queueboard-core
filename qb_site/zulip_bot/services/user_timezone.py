"""Resolve the timezone to interpret a reviewer's local times in.

Lives in ``zulip_bot`` because the authoritative source is Zulip's own user record (which Zulip
derives from the user's browser); ``core`` stays free of app dependencies. Consumed by the reviewer
console's preferences page, where it decides what the naive ``datetime-local`` ``away_until`` input
means (design doc 022).

Resolution order: Zulip's reported timezone → ``core.User.timezone`` → the Django default. A missing
Zulip link (or an unconfigured/unreachable Zulip) simply falls through; it is never an error. Note
``core.User.timezone`` is currently only ever set by hand in the Django admin, so in practice the
fallback is the project default until we persist a browser-reported zone (022, deferred).
"""

from __future__ import annotations

import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.utils import timezone

from core.models import User
from zulip_bot.services.zulip_client import ZulipApiError, ZulipClient

logger = logging.getLogger(__name__)


def resolve_user_timezone_name(*, user: User, zulip_user_id: int | None = None) -> str:
    """Return an IANA timezone name for ``user``, preferring what Zulip reports.

    ``zulip_user_id`` defaults to the user's own linked Zulip id; pass it explicitly when the caller
    holds a verified id from elsewhere (the prefs token carries one).
    """
    lookup_id = zulip_user_id if zulip_user_id is not None else user.zulip_user_id
    if lookup_id is not None:
        zulip_tz_name = fetch_zulip_user_timezone_name(int(lookup_id))
        if zulip_tz_name:
            return zulip_tz_name
    if user.timezone and is_valid_timezone_name(user.timezone):
        return user.timezone
    return timezone.get_default_timezone_name()


def fetch_zulip_user_timezone_name(zulip_user_id: int) -> str | None:
    base_url = getattr(settings, "ZULIP_BASE_URL", "").strip()
    bot_email = getattr(settings, "ZULIP_BOT_EMAIL", "").strip()
    bot_api_key = getattr(settings, "ZULIP_BOT_API_KEY", "").strip()
    if not base_url or not bot_email or not bot_api_key:
        return None
    try:
        payload = ZulipClient().get_user_by_id(zulip_user_id)
    except ZulipApiError:
        logger.exception("zulip_timezone_lookup_failed", extra={"zulip_user_id": zulip_user_id})
        return None
    user = payload.get("user")
    if not isinstance(user, dict):
        return None
    timezone_name = user.get("timezone")
    if isinstance(timezone_name, str) and is_valid_timezone_name(timezone_name):
        return timezone_name
    return None


def is_valid_timezone_name(value: str) -> bool:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError:
        return False
    return True


__all__ = [
    "resolve_user_timezone_name",
    "fetch_zulip_user_timezone_name",
    "is_valid_timezone_name",
]
