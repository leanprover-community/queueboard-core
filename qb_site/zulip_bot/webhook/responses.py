from __future__ import annotations

from django.http import JsonResponse

from zulip_bot.commands import CommandContext, CommandResult, ResponseMode, list_commands
from zulip_bot.webhook.payload import ParsedPayload


def unknown_command_help_response(name: str, context: CommandContext) -> CommandResult:
    location = "this DM" if context.is_private else "this channel"
    lines = [f"Unknown command: {name}", "", f"Commands available to you in {location}:"]
    commands = [command for command in list_commands() if command.name in context.allowed_command_names]
    for command in commands:
        lines.append(f"- {command.name}: {command.description}")
    return CommandResult(content="\n".join(lines), response_mode=ResponseMode.PRIVATE)


def invalid_payload_response(errors: tuple[str, ...], parsed: ParsedPayload) -> JsonResponse:
    return JsonResponse(
        {
            "error": "Invalid payload",
            "errors": list(errors),
            "received_payload": parsed.payload,
            "raw_payload": parsed.raw_body,
        },
        status=400,
    )


def ignored_response() -> JsonResponse:
    return JsonResponse({"response_not_required": True})


def zulip_response(result: CommandResult, override_mode: ResponseMode | None = None) -> JsonResponse:
    if result.response_not_required:
        return ignored_response()
    response_mode = override_mode or result.response_mode
    return JsonResponse(
        {
            "content": result.content,
            "type": response_mode.value,
        }
    )
