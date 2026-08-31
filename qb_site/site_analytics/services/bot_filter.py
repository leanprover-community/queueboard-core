"""Bot/crawler user-agent filtering for analytics ingestion."""

from __future__ import annotations

# Case-insensitive substrings that identify known bots/crawlers/tools.
# Extend conservatively; false positives silently drop legitimate pageviews.
_BOT_UA_SUBSTRINGS: tuple[str, ...] = (
    "bot",
    "crawler",
    "spider",
    "scraper",
    "curl/",
    "wget/",
    "python-requests",
    "python-urllib",
    "go-http-client",
    "java/",
    "libwww",
    "httpclient",
    "okhttp",
    "axios/",
    "node-fetch",
    "got/",
    "undici",
    "vercel",
)


def is_bot(user_agent: str) -> bool:
    """Return True if the user-agent matches a known bot/crawler pattern.

    Empty user-agents are allowed (not treated as bots) in v1; that behaviour
    can be tightened via a settings flag in a later chunk.
    """
    if not user_agent:
        return False
    ua_lower = user_agent.lower()
    return any(sub in ua_lower for sub in _BOT_UA_SUBSTRINGS)
