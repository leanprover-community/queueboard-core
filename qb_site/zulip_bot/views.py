from __future__ import annotations

import logging
from dataclasses import replace

from django import forms
from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.template.response import TemplateResponse
from django.views.decorators.csrf import csrf_exempt

from zulip_bot.commands import get_command
from zulip_bot.commands import echo as _echo  # noqa: F401
from zulip_bot.commands import help as _help  # noqa: F401
from zulip_bot.commands import prefs as _prefs  # noqa: F401
from zulip_bot.services.prefs_links import PrefsTokenExpired, PrefsTokenInvalid, validate_prefs_token
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
from zulip_bot.webhook.sender import SenderClassifier

logger = logging.getLogger(__name__)


class DummyPrefsForm(forms.Form):
    notes = forms.CharField(
        required=False,
        label="Dummy notes",
        widget=forms.Textarea(attrs={"rows": 4, "cols": 60}),
    )


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

    sender_classifier = SenderClassifier()
    if sender_classifier.is_bot_sender(parsed_payload.payload):
        logger.info("zulip_command_ignored", extra={"reason": "bot_sender"})
        return ignored_response()

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


def prefs_form(request: HttpRequest, token: str) -> HttpResponse:
    try:
        claims = validate_prefs_token(token)
    except PrefsTokenExpired:
        return _prefs_invalid_response(request, reason="expired")
    except PrefsTokenInvalid:
        return _prefs_invalid_response(request, reason="invalid")

    if request.method == "POST":
        form = DummyPrefsForm(request.POST)
        submitted = False
        if form.is_valid():
            submitted = True
            form = DummyPrefsForm()
    else:
        form = DummyPrefsForm()
        submitted = False

    response = TemplateResponse(
        request,
        "zulip_bot/prefs_form.html",
        {
            "form": form,
            "submitted": submitted,
            "claims": claims,
            "ttl_minutes": int(getattr(settings, "ZULIP_PREFS_TOKEN_TTL_SECONDS", 1800) / 60),
        },
        status=200,
    )
    response["Cache-Control"] = "no-store"
    return response


def _prefs_invalid_response(request: HttpRequest, *, reason: str) -> HttpResponse:
    response = TemplateResponse(
        request,
        "zulip_bot/prefs_invalid.html",
        {"reason": reason},
        status=403,
    )
    response["Cache-Control"] = "no-store"
    return response
