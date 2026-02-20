from __future__ import annotations

from typing import Any

from zulip_bot.commands import CommandContext


def build_context(payload: dict[str, Any]) -> CommandContext:
    message = payload.get("message") or {}
    return CommandContext(
        sender_id=message.get("sender_id"),
        sender_email=message.get("sender_email"),
        sender_full_name=message.get("sender_full_name"),
        message_content=message.get("content", ""),
        rendered_content=message.get("rendered_content"),
        message_id=message.get("id"),
        stream_id=message.get("stream_id"),
        topic=message.get("subject"),
        is_private=message.get("type") == "private",
    )
