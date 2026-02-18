from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.db.models import Case, IntegerField, When
from django.forms import modelformset_factory
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, JsonResponse
from django.urls import reverse
from django.template.response import TemplateResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from core.models import ReviewerPreference, User
from zulip_bot.commands import CommandResult, ResponseMode, get_command
from zulip_bot.commands import echo as _echo  # noqa: F401
from zulip_bot.commands import help as _help  # noqa: F401
from zulip_bot.commands import prefs as _prefs  # noqa: F401
from zulip_bot.commands import register_test as _register_test  # noqa: F401
from zulip_bot.forms import ReviewerPreferenceForm
from zulip_bot.services.registration_bootstrap import ensure_default_preferences_for_user
from zulip_bot.services.github_oauth import GitHubOAuthClient, GitHubOAuthError
from zulip_bot.services.prefs_links import (
    PrefsLinkClaims,
    PrefsTokenExpired,
    PrefsTokenInvalid,
    build_prefs_link,
    validate_prefs_token,
)
from zulip_bot.services.registration_links import (
    RegistrationTokenExpired,
    RegistrationTokenInvalid,
    validate_registration_token,
)
from zulip_bot.services.registration_linking import RegistrationLinkConflict, link_or_create_user_from_registration
from zulip_bot.services.registration_oauth_state import (
    RegistrationOAuthStateExpired,
    RegistrationOAuthStateInvalid,
    RegistrationOAuthStateClaims,
    issue_registration_oauth_state,
    validate_registration_oauth_state,
)
from zulip_bot.services.zulip_client import ZulipApiError, ZulipClient
from zulip_bot.webhook.context import build_context
from zulip_bot.webhook.membership import GroupMembershipChecker
from zulip_bot.webhook.payload import (
    has_leading_bot_mention,
    parse_command,
    parse_payload,
    strip_leading_bot_mention,
    validate_payload,
)
from zulip_bot.webhook.policy import allowed_command_names
from zulip_bot.webhook.responses import (
    ignored_response,
    invalid_payload_response,
    unknown_command_help_response,
    zulip_response,
)
from zulip_bot.webhook.sender import SenderClassifier

logger = logging.getLogger(__name__)
ReviewerPreferenceFormSet = modelformset_factory(
    ReviewerPreference,
    form=ReviewerPreferenceForm,
    extra=0,
    can_delete=False,
)


@csrf_exempt
def webhook(request: HttpRequest) -> HttpResponse:
    try:
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
        if not context.is_private and not has_leading_bot_mention(context.message_content, parsed_payload.payload):
            logger.info("zulip_command_ignored", extra={"reason": "missing_leading_bot_mention"})
            return ignored_response()

        checker = GroupMembershipChecker()
        allowed_names = allowed_command_names(context, checker)
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.exception("zulip_webhook_unexpected_error")
        return zulip_response(_unexpected_error_response(exc), ResponseMode.PRIVATE)

    try:
        context = replace(context, allowed_command_names=allowed_names)

        command_content = context.message_content
        if not context.is_private:
            command_content = strip_leading_bot_mention(command_content, parsed_payload.payload)

        parsed_command = parse_command(command_content)
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
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.exception("zulip_command_unexpected_error")
        return zulip_response(_unexpected_error_response(exc), ResponseMode.PRIVATE)


def prefs_form(request: HttpRequest, token: str) -> HttpResponse:
    try:
        claims = validate_prefs_token(token)
    except PrefsTokenExpired:
        return _prefs_invalid_response(request, reason="expired")
    except PrefsTokenInvalid:
        return _prefs_invalid_response(request, reason="invalid")

    prefs = _load_authorized_preferences(claims.user_id, claims.zulip_user_id, claims.preference_ids)
    if not prefs:
        return _prefs_invalid_response(request, reason="invalid")

    user = prefs[0].user
    user_timezone_name = _resolve_user_timezone_name(user=user, zulip_user_id=claims.zulip_user_id)
    user_timezone = ZoneInfo(user_timezone_name)
    pref_ids = [pref.pk for pref in prefs]
    ordering = Case(
        *[When(pk=pref_id, then=pos) for pos, pref_id in enumerate(pref_ids)],
        output_field=IntegerField(),
    )
    queryset = (
        ReviewerPreference.objects.filter(
            pk__in=pref_ids,
            user_id=claims.user_id,
        )
        .select_related("repository", "user")
        .order_by(ordering)
    )

    submitted = request.method == "GET" and request.GET.get("saved") == "1"
    saved_at: datetime | None = None
    with timezone.override(user_timezone):
        if request.method == "POST":
            formset = ReviewerPreferenceFormSet(
                request.POST,
                queryset=queryset,
                form_kwargs={"user_timezone": user_timezone},
            )
            if formset.is_valid():
                formset.save()
                url = reverse("zulip-prefs-form", kwargs={"token": token})
                return HttpResponseRedirect(f"{url}?saved=1")
        else:
            formset = ReviewerPreferenceFormSet(
                queryset=queryset,
                form_kwargs={"user_timezone": user_timezone},
            )
        if submitted:
            saved_at = timezone.localtime(timezone.now(), user_timezone)

    expires_at_unix = claims.exp
    if expires_at_unix is None:
        return _prefs_invalid_response(request, reason="invalid")
    expires_at_utc = datetime.fromtimestamp(expires_at_unix, tz=dt_timezone.utc)

    response = TemplateResponse(
        request,
        "zulip_bot/prefs_form.html",
        {
            "formset": formset,
            "submitted": submitted,
            "saved_at": saved_at,
            "user": user,
            "expires_at_unix": expires_at_unix,
            "expires_at_iso": expires_at_utc.isoformat(),
            "user_timezone_name": user_timezone_name,
        },
        status=200,
    )
    response["Cache-Control"] = "no-store"
    return response


def register_start(request: HttpRequest, token: str) -> HttpResponse:
    try:
        claims = validate_registration_token(token)
    except RegistrationTokenExpired:
        return _register_invalid_response(request, reason="expired")
    except RegistrationTokenInvalid:
        return _register_invalid_response(request, reason="invalid")

    expires_at_unix = claims.exp
    if expires_at_unix is None:
        return _register_invalid_response(request, reason="invalid")
    expires_at_utc = datetime.fromtimestamp(expires_at_unix, tz=dt_timezone.utc)
    response = TemplateResponse(
        request,
        "zulip_bot/register_start.html",
        {
            "token": token,
            "claims": claims,
            "expires_at_unix": expires_at_unix,
            "expires_at_iso": expires_at_utc.isoformat(),
            "oauth_enabled": _github_oauth_is_configured(),
        },
        status=200,
    )
    response["Cache-Control"] = "no-store"
    return response


def register_github_start(request: HttpRequest, token: str) -> HttpResponse:
    try:
        claims = validate_registration_token(token)
    except RegistrationTokenExpired:
        return _register_invalid_response(request, reason="expired")
    except RegistrationTokenInvalid:
        return _register_invalid_response(request, reason="invalid")

    if not _github_oauth_is_configured():
        return _register_invalid_response(request, reason="oauth_unavailable")

    state = issue_registration_oauth_state(
        claims=RegistrationOAuthStateClaims(
            registration_token=token,
            registration_nonce=claims.nonce or "",
        )
    )
    redirect_uri = _github_oauth_redirect_uri(request)
    if not redirect_uri:
        return _register_invalid_response(request, reason="oauth_unavailable")
    try:
        auth_url = GitHubOAuthClient().build_authorize_url(state=state, redirect_uri=redirect_uri)
    except GitHubOAuthError:
        logger.exception("github_oauth_start_failed")
        return _register_invalid_response(request, reason="oauth_unavailable")
    return HttpResponseRedirect(auth_url)


def register_github_callback(request: HttpRequest) -> HttpResponse:
    state = (request.GET.get("state") or "").strip()
    code = (request.GET.get("code") or "").strip()
    if not state or not code:
        return _register_invalid_response(request, reason="oauth_invalid")
    try:
        state_claims = validate_registration_oauth_state(state)
    except RegistrationOAuthStateExpired:
        return _register_invalid_response(request, reason="expired")
    except RegistrationOAuthStateInvalid:
        return _register_invalid_response(request, reason="oauth_invalid")

    try:
        registration_claims = validate_registration_token(state_claims.registration_token)
    except RegistrationTokenExpired:
        return _register_invalid_response(request, reason="expired")
    except RegistrationTokenInvalid:
        return _register_invalid_response(request, reason="invalid")

    if registration_claims.nonce != state_claims.registration_nonce:
        return _register_invalid_response(request, reason="oauth_invalid")

    redirect_uri = _github_oauth_redirect_uri(request)
    if not redirect_uri:
        return _register_invalid_response(request, reason="oauth_unavailable")
    try:
        oauth_client = GitHubOAuthClient()
        access_token = oauth_client.exchange_code_for_access_token(code=code, redirect_uri=redirect_uri)
        identity = oauth_client.fetch_user_identity(access_token=access_token)
    except GitHubOAuthError:
        logger.exception("github_oauth_callback_failed")
        return _register_invalid_response(request, reason="oauth_failed")
    try:
        link_result = link_or_create_user_from_registration(
            zulip_user_id=registration_claims.zulip_user_id,
            zulip_full_name=registration_claims.sender_full_name,
            identity=identity,
        )
    except RegistrationLinkConflict:
        logger.info("registration_link_conflict", extra={"reason": "link_conflict"})
        return _register_invalid_response(request, reason="link_conflict")
    bootstrap_result = ensure_default_preferences_for_user(user=link_result.user)
    prefs_link, prefs_expires_unix, prefs_expires_iso = _build_prefs_link_for_user(
        user=link_result.user,
        zulip_user_id=registration_claims.zulip_user_id,
    )
    dm_sent = _send_registration_success_dm(
        zulip_user_id=registration_claims.zulip_user_id,
        github_login=identity.github_login,
        prefs_link=prefs_link,
        prefs_expires_unix=prefs_expires_unix,
    )

    response = TemplateResponse(
        request,
        "zulip_bot/register_callback.html",
        {
            "registration_claims": registration_claims,
            "identity": identity,
            "link_result": link_result,
            "bootstrap_result": bootstrap_result,
            "prefs_link": prefs_link,
            "prefs_expires_unix": prefs_expires_unix,
            "prefs_expires_iso": prefs_expires_iso,
            "dm_sent": dm_sent,
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


def _register_invalid_response(request: HttpRequest, *, reason: str) -> HttpResponse:
    response = TemplateResponse(
        request,
        "zulip_bot/register_invalid.html",
        {"reason": reason},
        status=403,
    )
    response["Cache-Control"] = "no-store"
    return response


def _build_prefs_link_for_user(*, user: User, zulip_user_id: int) -> tuple[str | None, int | None, str | None]:
    preference_ids = tuple(ReviewerPreference.objects.filter(user_id=user.id).values_list("id", flat=True).order_by("id"))
    if not preference_ids:
        return (None, None, None)
    prefs_link = build_prefs_link(
        claims=PrefsLinkClaims(
            user_id=user.id,
            zulip_user_id=zulip_user_id,
            preference_ids=preference_ids,
        )
    )
    ttl_seconds = int(getattr(settings, "ZULIP_PREFS_TOKEN_TTL_SECONDS", 1800))
    expires_at = timezone.now() + timedelta(seconds=ttl_seconds)
    expires_unix = int(expires_at.timestamp())
    return (prefs_link, expires_unix, datetime.fromtimestamp(expires_unix, tz=dt_timezone.utc).isoformat())


def _send_registration_success_dm(
    *,
    zulip_user_id: int,
    github_login: str,
    prefs_link: str | None,
    prefs_expires_unix: int | None,
) -> bool:
    if prefs_link and prefs_expires_unix:
        content = (
            f"Successfully linked your Zulip account with GitHub user `{github_login}`.\n\n"
            f"Next step: click this private link to [finalize your reviewer preferences]({prefs_link}). "
            f"It expires at <time:{prefs_expires_unix}>."
        )
    else:
        content = (
            f"Successfully linked your Zulip account with GitHub user `{github_login}`.\n\n"
            "You do not currently have any reviewer preferences to edit."
        )
    try:
        ZulipClient().send_direct_message(to=[zulip_user_id], content=content)
    except ZulipApiError:
        logger.exception("registration_success_dm_failed", extra={"zulip_user_id": zulip_user_id})
        return False
    return True


def _github_oauth_is_configured() -> bool:
    client_id = getattr(settings, "GITHUB_OAUTH_CLIENT_ID", "").strip()
    client_secret = getattr(settings, "GITHUB_OAUTH_CLIENT_SECRET", "").strip()
    return bool(client_id and client_secret)


def _github_oauth_redirect_uri(request: HttpRequest) -> str:
    configured = getattr(settings, "GITHUB_OAUTH_REDIRECT_URI", "").strip()
    if configured:
        return configured
    return request.build_absolute_uri(reverse("zulip-register-github-callback"))


def _load_authorized_preferences(user_id: int, zulip_user_id: int, preference_ids: tuple[int, ...]) -> list[ReviewerPreference]:
    pref_ids = tuple(dict.fromkeys(preference_ids))
    if not pref_ids:
        return []
    prefs = list(ReviewerPreference.objects.filter(id__in=pref_ids).select_related("repository", "user"))
    if len(prefs) != len(pref_ids):
        return []
    by_id = {pref.id: pref for pref in prefs}
    ordered = [by_id[pref_id] for pref_id in pref_ids]
    if any(pref.user_id != user_id for pref in ordered):
        return []
    if any(pref.user.zulip_user_id not in (None, zulip_user_id) for pref in ordered):
        return []
    return ordered


def _resolve_user_timezone_name(*, user: User, zulip_user_id: int) -> str:
    zulip_tz_name = _fetch_zulip_user_timezone_name(zulip_user_id)
    if zulip_tz_name:
        return zulip_tz_name
    if user.timezone and _is_valid_timezone_name(user.timezone):
        return user.timezone
    return timezone.get_default_timezone_name()


def _fetch_zulip_user_timezone_name(zulip_user_id: int) -> str | None:
    base_url = getattr(settings, "ZULIP_BASE_URL", "").strip()
    bot_email = getattr(settings, "ZULIP_BOT_EMAIL", "").strip()
    bot_api_key = getattr(settings, "ZULIP_BOT_API_KEY", "").strip()
    if not base_url or not bot_email or not bot_api_key:
        return None
    try:
        payload = ZulipClient().get_user_by_id(zulip_user_id)
    except ZulipApiError:
        logger.exception("zulip_timezone_lookup_failed", extra={"zulip_user_id": zulip_user_id})
        return None
    user = payload.get("user")
    if not isinstance(user, dict):
        return None
    timezone_name = user.get("timezone")
    if isinstance(timezone_name, str) and _is_valid_timezone_name(timezone_name):
        return timezone_name
    return None


def _is_valid_timezone_name(value: str) -> bool:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError:
        return False
    return True


def _unexpected_error_response(exc: Exception) -> CommandResult:
    payload = {
        "error": "zulip_unexpected_error",
        "message": str(exc),
        "error_type": type(exc).__name__,
        "details": _error_details(exc),
    }
    details_json = json.dumps(payload, indent=2, sort_keys=True)
    content = (
        "An unexpected error occurred while processing this command.\n\n"
        "````spoiler detailed error info\n"
        "```json\n"
        f"{details_json}\n"
        "```\n"
        "````"
    )
    return CommandResult(content=content, response_mode=ResponseMode.PRIVATE)


def _error_details(exc: Exception) -> Any:
    payload = getattr(exc, "payload", None)
    if isinstance(payload, dict):
        return payload
    return None
