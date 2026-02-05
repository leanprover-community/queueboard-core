from __future__ import annotations

import json
from dataclasses import dataclass

from django.conf import settings
from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from zulip_bot.commands import CommandContext, CommandResult, ResponseMode, get_command
from zulip_bot.commands import echo as _echo  # noqa: F401
from zulip_bot.commands import help as _help  # noqa: F401


@dataclass(frozen=True)
class ParsedCommand:
    name: str
    args: str


def _parse_payload(request: HttpRequest) -> dict:
    if request.content_type and request.content_type.startswith("application/json"):
        try:
            return json.loads(request.body.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    if "payload" in request.POST:
        try:
            return json.loads(request.POST["payload"])
        except json.JSONDecodeError:
            return {}

    return dict(request.POST)


def _parse_command(message_content: str) -> ParsedCommand | None:
    content = message_content.strip()
    if not content:
        return None

    if content.startswith("/"):
        content = content[1:]

    parts = content.split(maxsplit=1)
    name = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""
    return ParsedCommand(name=name, args=args)


def _build_context(payload: dict) -> CommandContext:
    message = payload.get("message") or {}
    return CommandContext(
        sender_id=payload.get("sender_id"),
        sender_email=payload.get("sender_email"),
        sender_full_name=payload.get("sender_full_name"),
        message_content=message.get("content", ""),
        message_id=message.get("id"),
        stream_id=message.get("stream_id"),
        topic=message.get("subject"),
        is_private=message.get("type") == "private",
    )


def _unknown_command_response(name: str | None) -> CommandResult:
    if name:
        content = f"Unknown command: {name}. Try 'help'."
    else:
        content = "No command found. Try 'help'."
    return CommandResult(content=content, response_mode=ResponseMode.PRIVATE)


@csrf_exempt
def webhook(request: HttpRequest) -> JsonResponse:
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    payload = _parse_payload(request)
    token = payload.get("token")
    expected_token = getattr(settings, "ZULIP_WEBHOOK_TOKEN", None)
    if not expected_token or token != expected_token:
        return JsonResponse({"error": "Forbidden"}, status=403)

    context = _build_context(payload)
    parsed = _parse_command(context.message_content)
    if parsed is None:
        result = _unknown_command_response(None)
        return _zulip_response(result)

    command = get_command(parsed.name)
    if not command:
        result = _unknown_command_response(parsed.name)
        return _zulip_response(result)

    result = command.handler(context, parsed.args)
    return _zulip_response(result, command.response_mode)


def _zulip_response(result: CommandResult, override_mode: ResponseMode | None = None) -> JsonResponse:
    response_mode = override_mode or result.response_mode
    return JsonResponse(
        {
            "content": result.content,
            "type": response_mode.value,
        }
    )
