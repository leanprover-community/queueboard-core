from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from django.http import HttpRequest

MENTION_PREFIX_RE = re.compile(r"^@\*\*.+?\*\*(?:[:,]\s*|\s+|$)")


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

    content = _strip_leading_mentions(content)
    if not content:
        return None

    if content.startswith("/"):
        content = content[1:]

    parts = content.split(maxsplit=1)
    name = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""
    return ParsedCommand(name=name, args=args)


def _strip_leading_mentions(content: str) -> str:
    while True:
        match = MENTION_PREFIX_RE.match(content)
        if not match:
            return content
        content = content[match.end() :].lstrip()
