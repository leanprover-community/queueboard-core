from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.http import HttpRequest

MENTION_PREFIX_RE = re.compile(r"^@\*\*(?P<name>.+?)\*\*(?:[:,]\s*|\s+|$)")


@dataclass(frozen=True)
class ParsedCommand:
    name: str
    args: str


@dataclass(frozen=True)
class ParsedPayload:
    payload: dict[str, Any] | None
    errors: tuple[str, ...] = ()
    raw_body: str | None = None


def parse_payload(request: HttpRequest) -> ParsedPayload:
    raw_body = request.body.decode("utf-8", errors="replace") if request.body else ""

    if request.content_type and request.content_type.startswith("application/json"):
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            return ParsedPayload(
                payload=None,
                errors=(f"invalid_json:{exc.msg}",),
                raw_body=raw_body,
            )
        if not isinstance(payload, dict):
            return ParsedPayload(
                payload=None,
                errors=("invalid_payload:json_root_must_be_object",),
                raw_body=raw_body,
            )
        return ParsedPayload(payload=payload, raw_body=raw_body)

    if "payload" in request.POST:
        raw_payload = request.POST.get("payload", "")
        try:
            payload = json.loads(raw_payload)
        except json.JSONDecodeError as exc:
            return ParsedPayload(
                payload=None,
                errors=(f"invalid_form_payload_json:{exc.msg}",),
                raw_body=raw_payload,
            )
        if not isinstance(payload, dict):
            return ParsedPayload(
                payload=None,
                errors=("invalid_payload:form_payload_root_must_be_object",),
                raw_body=raw_payload,
            )
        return ParsedPayload(payload=payload, raw_body=raw_payload)

    return ParsedPayload(payload=dict(request.POST), raw_body=raw_body)


def validate_payload(payload: dict[str, Any]) -> tuple[str, ...]:
    errors: list[str] = []
    message = payload.get("message")
    if not isinstance(message, dict):
        errors.append("missing_or_invalid_field:message")
    else:
        if not isinstance(message.get("content"), str):
            errors.append("missing_or_invalid_field:message.content")
        if not isinstance(message.get("id"), int):
            errors.append("missing_or_invalid_field:message.id")
        if not isinstance(message.get("sender_id"), int):
            errors.append("missing_or_invalid_field:message.sender_id")
        if not isinstance(message.get("sender_email"), str):
            errors.append("missing_or_invalid_field:message.sender_email")
        if not isinstance(message.get("sender_full_name"), str):
            errors.append("missing_or_invalid_field:message.sender_full_name")
        message_type = message.get("type")
        if message_type not in {"private", "stream"}:
            errors.append("missing_or_invalid_field:message.type")
        if message_type == "stream" and not isinstance(message.get("stream_id"), int):
            errors.append("missing_or_invalid_field:message.stream_id")
    return tuple(errors)


def parse_command(message_content: str) -> ParsedCommand | None:
    content = message_content.strip()
    if not content:
        return None

    mention_match = MENTION_PREFIX_RE.match(content)
    if mention_match and mention_match.end() == len(content):
        return None

    if content.startswith("/"):
        content = content[1:]

    parts = content.split(maxsplit=1)
    name = parts[0].lower().replace("_", "-")
    args = parts[1] if len(parts) > 1 else ""
    return ParsedCommand(name=name, args=args)


def has_leading_bot_mention(message_content: str, payload: dict[str, Any]) -> bool:
    content = message_content.lstrip()
    match = MENTION_PREFIX_RE.match(content)
    if not match:
        return False

    mentioned_name = _normalize_mention_target(match.group("name"))
    if not mentioned_name:
        return False

    bot_identifiers = _bot_mention_identifiers(payload)
    if not bot_identifiers:
        return False

    return mentioned_name in bot_identifiers


def strip_leading_bot_mention(message_content: str, payload: dict[str, Any]) -> str:
    content = message_content.lstrip()
    match = MENTION_PREFIX_RE.match(content)
    if not match:
        return content

    mentioned_name = _normalize_mention_target(match.group("name"))
    if not mentioned_name:
        return content

    bot_identifiers = _bot_mention_identifiers(payload)
    if mentioned_name not in bot_identifiers:
        return content

    return content[match.end() :].lstrip()


def _bot_mention_identifiers(payload: dict[str, Any]) -> set[str]:
    identifiers: set[str] = set()

    bot_full_name = payload.get("bot_full_name")
    if isinstance(bot_full_name, str):
        normalized = _normalize_mention_target(bot_full_name)
        if normalized:
            identifiers.add(normalized)

    bot_email = payload.get("bot_email")
    if isinstance(bot_email, str):
        local_part = _email_local_part(bot_email)
        if local_part:
            identifiers.add(local_part)

    configured_email = str(getattr(settings, "ZULIP_BOT_EMAIL", "")).strip()
    local_part = _email_local_part(configured_email)
    if local_part:
        identifiers.add(local_part)

    return identifiers


def _email_local_part(value: str) -> str:
    email = value.strip()
    if not email:
        return ""
    return email.split("@", 1)[0].strip().lower()


def _normalize_mention_target(value: str) -> str:
    raw_target = value.strip()
    if not raw_target:
        return ""
    target_without_id = raw_target.split("|", 1)[0]
    normalized = re.sub(r"\s+", " ", target_without_id.strip()).lower()
    return normalized
