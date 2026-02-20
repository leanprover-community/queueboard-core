from __future__ import annotations

from zulip_bot.commands import CommandContext, CommandResult, ResponseMode, register_command
from zulip_bot.services.assignment_execution import run_assignment_command


@register_command(
    name="unassign",
    description="Validate unassignment targets for a pull request.",
    response_mode=ResponseMode.PRIVATE,
)
def unassign_command(context: CommandContext, args: str) -> CommandResult:
    return run_assignment_command(action="unassign", context=context, args=args)
