from __future__ import annotations

from zulip_bot.commands import CommandContext, CommandResult, register_command


@register_command(
    name="echo",
    description="Repeat the provided text.",
)
def echo_command(context: CommandContext, args: str) -> CommandResult:
    message = args.strip()
    if not message:
        message = "(no content)"
    return CommandResult(content=message)
