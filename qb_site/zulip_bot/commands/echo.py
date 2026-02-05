from __future__ import annotations

from zulip_bot.commands import CommandContext, CommandResult, ResponseMode, register_command


@register_command(
    name="echo",
    description="Repeat the provided text.",
    response_mode=ResponseMode.PRIVATE,
)
def echo_command(context: CommandContext, args: str) -> CommandResult:
    message = args.strip()
    if not message:
        message = "(no content)"
    return CommandResult(content=message, response_mode=ResponseMode.PRIVATE)
