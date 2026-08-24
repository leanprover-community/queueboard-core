from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from core.utils.zulip_time import format_global_time
from zulip_bot.commands import CommandContext, CommandResult, register_command
from zulip_bot.services.registration_links import RegistrationLinkClaims, build_registration_link
from zulip_bot.services.zulip_client import ZulipApiError, ZulipClient

logger = logging.getLogger(__name__)


@register_command(
    name="register-test",
    description="Get a private registration test link for GitHub OAuth.",
)
def register_test_command(context: CommandContext, args: str) -> CommandResult:
    del args  # command currently has no arguments

    if context.sender_id is None:
        return CommandResult(
            content="Could not determine your Zulip identity from this message.",
        )

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
        f"Use this private link to [test registration via GitHub OAuth]({register_link}). "
        f"It expires at {format_global_time(expires_at)}."
    )
    # Private links must be delivered via send_direct_message. See close_pr.py
    # for the rationale: Zulip webhook responses always go back to the triggering
    # conversation, so returning content via CommandResult would expose it in a stream.
    try:
        ZulipClient().send_direct_message(to=[context.sender_id], content=dm_content)
    except ZulipApiError as exc:
        logger.exception("register_test_dm_failed")
        return CommandResult(content=f"Failed to send private registration link: {exc.message}")
    return CommandResult(response_not_required=True)
