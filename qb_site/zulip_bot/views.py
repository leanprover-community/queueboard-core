from __future__ import annotations

import logging
from dataclasses import replace

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from zulip_bot.commands import get_command
from zulip_bot.commands import echo as _echo  # noqa: F401
from zulip_bot.commands import help as _help  # noqa: F401
from zulip_bot.webhook.context import build_context
from zulip_bot.webhook.membership import GroupMembershipChecker
from zulip_bot.webhook.payload import parse_command, parse_payload, validate_payload
from zulip_bot.webhook.policy import allowed_command_names
from zulip_bot.webhook.responses import (
    ignored_response,
    invalid_payload_response,
    unknown_command_help_response,
    zulip_response,
)

logger = logging.getLogger(__name__)


@csrf_exempt
def webhook(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    parsed_payload = parse_payload(request)
    if parsed_payload.payload is None:
        return invalid_payload_response(parsed_payload.errors, parsed_payload)

    payload_errors = validate_payload(parsed_payload.payload)
    if payload_errors:
        return invalid_payload_response(payload_errors, parsed_payload)

    token = parsed_payload.payload.get("token")
    expected_token = getattr(settings, "ZULIP_WEBHOOK_TOKEN", None)
    if not expected_token or token != expected_token:
        return JsonResponse({"error": "Forbidden"}, status=403)

    context = build_context(parsed_payload.payload)
    checker = GroupMembershipChecker()
    allowed_names = allowed_command_names(context, checker)
    context = replace(context, allowed_command_names=allowed_names)

    parsed_command = parse_command(context.message_content)
    if parsed_command is None:
        return ignored_response()

    command = get_command(parsed_command.name)
    if command is None:
        if not allowed_names:
            return ignored_response()
        return zulip_response(unknown_command_help_response(parsed_command.name, context))

    if command.name not in allowed_names:
        logger.info("zulip_command_ignored", extra={"reason": "command_disallowed", "command": command.name})
        return ignored_response()

    result = command.handler(context, parsed_command.args)
    return zulip_response(result, command.response_mode)
