from __future__ import annotations

from zulip_bot.commands import CommandContext, CommandResult, ResponseMode, register_command
from zulip_bot.services.registration_links import RegistrationLinkClaims, build_registration_link


@register_command(
    name="register_test",
    description="Get a private registration test link for GitHub OAuth.",
    response_mode=ResponseMode.PRIVATE,
)
def register_test_command(context: CommandContext, args: str) -> CommandResult:
    del args  # command currently has no arguments

    if context.sender_id is None:
        return CommandResult(
            content="Could not determine your Zulip identity from this message.",
            response_mode=ResponseMode.PRIVATE,
        )

    register_link = build_registration_link(
        claims=RegistrationLinkClaims(
            zulip_user_id=context.sender_id,
            sender_email=context.sender_email,
            sender_full_name=context.sender_full_name,
        )
    )
    return CommandResult(
        content=f"Use this private link to [test registration via GitHub OAuth]({register_link}).",
        response_mode=ResponseMode.PRIVATE,
    )
