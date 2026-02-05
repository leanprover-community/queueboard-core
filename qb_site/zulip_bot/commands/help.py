from __future__ import annotations

from zulip_bot.commands import CommandContext, CommandResult, ResponseMode, list_commands, register_command


@register_command(
    name="help",
    description="List supported commands.",
    response_mode=ResponseMode.PRIVATE,
)
def help_command(context: CommandContext, args: str) -> CommandResult:
    commands = list_commands()
    lines = ["Available commands:"]
    for command in commands:
        lines.append(f"- {command.name}: {command.description}")
    return CommandResult(content="\n".join(lines), response_mode=ResponseMode.PRIVATE)
