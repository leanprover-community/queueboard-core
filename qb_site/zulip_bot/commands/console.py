from __future__ import annotations

from django.urls import reverse

from core.services.site_urls import build_site_url
from zulip_bot.commands import CommandContext, CommandResult, register_command


@register_command(
    name="console",
    description="Get a link to the reviewer console (accept or decline assignment proposals).",
)
def console_command(context: CommandContext, args: str) -> CommandResult:
    # The console URL is stable, token-less, and identical for every reviewer — the page
    # self-authenticates via GitHub OAuth (design doc 050). Nothing here is sender-specific
    # or secret, so we reply in place rather than DMing a private link (cf. commands/prefs.py).
    del context, args
    console_url = build_site_url(reverse("console:home"))
    return CommandResult(
        content=(
            f"Open the [reviewer console]({console_url}) to accept or decline the assignment "
            "proposals made to you. Sign in with GitHub — the link is stable and bookmarkable."
        )
    )
