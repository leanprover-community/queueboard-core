from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from core.models import ReviewerPreference, User
from zulip_bot.commands import CommandContext, CommandResult, ResponseMode, register_command
from zulip_bot.services.prefs_links import PrefsLinkClaims, build_prefs_link
from zulip_bot.services.registration_links import RegistrationLinkClaims, build_registration_link


@register_command(
    name="prefs",
    description="Get a private link to review preferences.",
    response_mode=ResponseMode.PRIVATE,
)
def prefs_command(context: CommandContext, args: str) -> CommandResult:
    del args  # command currently has no arguments

    if context.sender_id is None:
        return CommandResult(
            content="Could not determine your Zulip identity from this message.",
            response_mode=ResponseMode.PRIVATE,
        )

    user = User.objects.filter(zulip_user_id=context.sender_id).only("id", "zulip_user_id").first()
    if user is None:
        register_link = build_registration_link(
            claims=RegistrationLinkClaims(
                zulip_user_id=context.sender_id,
                sender_email=context.sender_email,
                sender_full_name=context.sender_full_name,
            )
        )
        return CommandResult(
            content=(
                "No reviewer profile is linked to your Zulip account yet. "
                f"Use this private link to [start registration]({register_link})."
            ),
            response_mode=ResponseMode.PRIVATE,
        )

    preference_ids = tuple(ReviewerPreference.objects.filter(user_id=user.id).values_list("id", flat=True).order_by("id"))
    if not preference_ids:
        return CommandResult(
            content="You do not currently have any reviewer preferences to edit.",
            response_mode=ResponseMode.PRIVATE,
        )

    link = build_prefs_link(
        claims=PrefsLinkClaims(
            user_id=user.id,
            zulip_user_id=context.sender_id,
            preference_ids=preference_ids,
        )
    )
    ttl_seconds = int(getattr(settings, "ZULIP_PREFS_TOKEN_TTL_SECONDS", 1800))
    expires_at = timezone.now() + timedelta(seconds=ttl_seconds)
    expires_unix = int(expires_at.timestamp())
    return CommandResult(
        content=(f"Use this private link to [open your reviewer preferences form]({link}). It expires at <time:{expires_unix}>."),
        response_mode=ResponseMode.PRIVATE,
    )
