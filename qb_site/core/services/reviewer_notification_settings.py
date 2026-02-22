from __future__ import annotations

from dataclasses import dataclass
from typing import Any


DEFAULT_STALE_NUDGE_DAYS = 14
DEFAULT_AUTO_UNASSIGN_DAYS = 21
MAX_AUTO_UNASSIGN_DAYS = 21


@dataclass(frozen=True)
class ReviewerNotificationPolicy:
    stale_nudge_days: int
    auto_unassign_days: int


def parse_notification_policy(settings_payload: Any) -> ReviewerNotificationPolicy:
    """Parse and normalize reviewer notification settings.

    Invalid or missing values fall back to defaults. The returned thresholds always satisfy:
    - stale_nudge_days >= 1
    - auto_unassign_days > stale_nudge_days
    """

    if not isinstance(settings_payload, dict):
        settings_payload = {}

    stale_nudge_days = _parse_positive_int(settings_payload.get("stale_nudge_days"), DEFAULT_STALE_NUDGE_DAYS)
    auto_unassign_days = _parse_positive_int(settings_payload.get("auto_unassign_days"), DEFAULT_AUTO_UNASSIGN_DAYS)

    # Hard cap for enforcement policy.
    auto_unassign_days = min(auto_unassign_days, MAX_AUTO_UNASSIGN_DAYS)
    # stale nudge must leave at least one day before auto-unassign.
    stale_nudge_days = min(stale_nudge_days, MAX_AUTO_UNASSIGN_DAYS - 1)
    if auto_unassign_days <= stale_nudge_days:
        auto_unassign_days = min(stale_nudge_days + 1, MAX_AUTO_UNASSIGN_DAYS)

    return ReviewerNotificationPolicy(
        stale_nudge_days=stale_nudge_days,
        auto_unassign_days=auto_unassign_days,
    )


def _parse_positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if parsed < 1:
        return default
    return parsed
