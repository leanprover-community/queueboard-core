from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from django.http import HttpRequest


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
        message_type = message.get("type")
        if message_type not in {"private", "stream"}:
            errors.append("missing_or_invalid_field:message.type")
    return tuple(errors)


def parse_command(message_content: str) -> ParsedCommand | None:
    content = message_content.strip()
    if not content:
        return None

    if content.startswith("/"):
        content = content[1:]

    parts = content.split(maxsplit=1)
    name = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""
    return ParsedCommand(name=name, args=args)
