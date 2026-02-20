from __future__ import annotations

from zulip_bot.commands import CommandContext, CommandResult, ResponseMode, register_command
from zulip_bot.services.assignment_preflight import run_assignment_preflight


@register_command(
    name="assign",
    description="Validate assignee targets for a pull request.",
    response_mode=ResponseMode.PRIVATE,
)
def assign_command(context: CommandContext, args: str) -> CommandResult:
    return run_assignment_preflight(action="assign", context=context, args=args)
