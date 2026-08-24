from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from core.models import ReviewerPreference, User
from core.utils.zulip_time import format_global_time
from zulip_bot.commands import CommandContext, CommandResult, register_command
from zulip_bot.services.prefs_links import PrefsLinkClaims, build_prefs_entry_link
from zulip_bot.services.registration_links import RegistrationLinkClaims, build_registration_link
from zulip_bot.services.zulip_client import ZulipApiError, ZulipClient

logger = logging.getLogger(__name__)


@register_command(
    name="prefs",
    description="Get a private link to review preferences.",
)
def prefs_command(context: CommandContext, args: str) -> CommandResult:
    del args  # command currently has no arguments

    if context.sender_id is None:
        return CommandResult(
            content="Could not determine your Zulip identity from this message.",
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
        ttl_seconds = int(getattr(settings, "ZULIP_REGISTRATION_TOKEN_TTL_SECONDS", 1800))
        expires_at = timezone.now() + timedelta(seconds=ttl_seconds)
        dm_content = (
            "No reviewer profile is linked to your Zulip account yet. "
            f"Use this private link to [start registration]({register_link}). "
            f"It expires at {format_global_time(expires_at)}."
        )
        return _send_dm(context.sender_id, dm_content, "prefs_registration_dm_failed")

    preference_ids = tuple(ReviewerPreference.objects.filter(user_id=user.id).values_list("id", flat=True).order_by("id"))
    if not preference_ids:
        return CommandResult(
            content="You do not currently have any reviewer preferences to edit.",
        )

    entry = build_prefs_entry_link(
        claims=PrefsLinkClaims(
            user_id=user.id,
            zulip_user_id=context.sender_id,
            preference_ids=preference_ids,
        )
    )
    if entry.is_stable:
        # The console URL is not secret (identical for everyone; the page self-authenticates), but
        # `prefs` stays a DM command anyway: an accidental mention in a public stream should not put
        # a reply there. Unlike `console`, which is an in-place reply by design (doc 050).
        dm_content = (
            f"Open your [reviewer preferences]({entry.url}) in the reviewer console. "
            "Sign in with GitHub — the link is stable, so you can bookmark it."
        )
        return _send_dm(context.sender_id, dm_content, "prefs_link_dm_failed")

    expires_at = timezone.now() + timedelta(seconds=int(getattr(settings, "ZULIP_PREFS_TOKEN_TTL_SECONDS", 1800)))
    dm_content = (
        f"Use this private link to [open your reviewer preferences form]({entry.url}). "
        f"It expires at {format_global_time(expires_at)}."
    )
    return _send_dm(context.sender_id, dm_content, "prefs_link_dm_failed")


def _send_dm(sender_id: int, content: str, log_event: str) -> CommandResult:
    # Private links must be delivered via send_direct_message. See close_pr.py
    # for the rationale: Zulip webhook responses always go back to the triggering
    # conversation, so returning content via CommandResult would expose it in a stream.
    try:
        ZulipClient().send_direct_message(to=[sender_id], content=content)
    except ZulipApiError as exc:
        logger.exception(log_event)
        return CommandResult(content=f"Failed to send private link: {exc.message}")
    return CommandResult(response_not_required=True)
