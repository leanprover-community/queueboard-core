from __future__ import annotations

from zulip_bot.commands import CommandContext, CommandResult
from zulip_bot.services.assignment_command_parser import AssignmentCommandParseError, parse_assignment_command_args
from zulip_bot.services.assignment_validation import validate_assignment_targets


def run_assignment_preflight(*, action: str, context: CommandContext, args: str) -> CommandResult:
    try:
        parsed = parse_assignment_command_args(
            args=args,
            rendered_content=context.rendered_content,
            sender_id=context.sender_id,
        )
    except AssignmentCommandParseError as exc:
        return CommandResult(content=f"Could not parse `{action}` command: {exc.message}")

    successes: list[str] = []
    warnings: list[str] = []
    failures: list[str] = []
    if parsed.unresolved_mentions:
        unresolved = ", ".join(parsed.unresolved_mentions)
        warnings.append(f"Unresolved mentions: {unresolved}.")

    validation = validate_assignment_targets(pr=parsed.pr, target_user_ids=parsed.target_user_ids)
    valid_targets = [target for target in validation.targets if target.ok]
    failed_targets = [target for target in validation.targets if not target.ok]

    if failed_targets:
        for target in failed_targets:
            failures.append(f"Zulip user {target.zulip_user_id}: {target.message} ({target.code})")

    if valid_targets:
        github_logins = ", ".join(sorted({target.github_login for target in valid_targets if target.github_login}))
        successes.append(f"Validated targets: `{github_logins}`.")
        successes.append(f"Preflight passed for `{action}` on {parsed.pr.owner}/{parsed.pr.repo}#{parsed.pr.number}.")
    else:
        failures.append(f"No valid reviewers to {action} after validation.")

    lines = [f"Preflight summary for `{action}`:"]
    if successes:
        lines.append("Successes:")
        lines.extend(f"- {entry}" for entry in successes)
    if warnings:
        lines.append("Warnings:")
        lines.extend(f"- {entry}" for entry in warnings)
    if failures:
        lines.append("Failures:")
        lines.extend(f"- {entry}" for entry in failures)
    lines.append("GitHub assignment mutation is not enabled yet in this rollout chunk.")

    return CommandResult(content="\n".join(lines))
