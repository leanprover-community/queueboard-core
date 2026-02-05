from __future__ import annotations

from zulip_bot.commands import CommandContext, CommandResult, ResponseMode, list_commands, register_command


@register_command(
    name="help",
    description="List supported commands.",
    response_mode=ResponseMode.PRIVATE,
)
def help_command(context: CommandContext, args: str) -> CommandResult:
    commands = list_commands()
    if context.allowed_command_names:
        commands = [command for command in commands if command.name in context.allowed_command_names]
    lines = ["Available commands:"]
    for command in commands:
        lines.append(f"- {command.name}: {command.description}")
    if len(lines) == 1:
        lines.append("- (no commands available in this context)")
    return CommandResult(content="\n".join(lines), response_mode=ResponseMode.PRIVATE)
