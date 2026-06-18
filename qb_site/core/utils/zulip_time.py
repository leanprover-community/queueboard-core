"""Helpers for rendering Zulip "global time" markdown tags.

Zulip's markdown ``<time:...>`` syntax renders an instant in each reader's local
timezone. As of Zulip 12.0 (server feature level 451) the markdown parser
resolves the value with :func:`datetime.datetime.fromisoformat` and no longer
accepts bare Unix timestamps -- an unparseable value is rendered as escaped
literal text (``&lt;time:...&gt;``) instead of a localized time. Always emit
ISO 8601 here.

References:
- https://github.com/zulip/zulip/pull/36837
- https://zulip.com/help/global-times
"""

from __future__ import annotations

from datetime import datetime, timezone


def format_global_time(value: datetime | int | float) -> str:
    """Return a Zulip ``<time:...>`` global-time tag for ``value`` using ISO 8601.

    ``value`` may be a timezone-aware ``datetime`` (a naive one is assumed UTC)
    or a Unix timestamp. The instant is normalized to UTC and formatted with
    second resolution, e.g. ``<time:2025-12-02T20:40:00+00:00>`` -- the same
    shape Zulip's own ``datetime_to_global_time`` helper emits.
    """
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromtimestamp(value, tz=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return f"<time:{dt.astimezone(timezone.utc).isoformat(timespec='seconds')}>"
