from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from core.models import User
from zulip_bot.commands import CommandContext, CommandResult, register_command
from zulip_bot.services.assignment_command_parser import AssignmentCommandParseError, _parse_single_pr_ref
from zulip_bot.services.close_pr_execution import PermissionOutcome, check_close_pr_permission
from zulip_bot.services.close_pr_links import ClosePRLinkClaims, build_close_pr_link
from zulip_bot.services.zulip_client import ZulipApiError, ZulipClient

logger = logging.getLogger(__name__)


@register_command(
    name="close-pr",
    description="Open a private confirmation form to close a pull request (requires GitHub write access or PR authorship).",
)
def close_pr_command(context: CommandContext, args: str) -> CommandResult:
    if context.sender_id is None:
        return CommandResult(
            content="Could not determine your Zulip identity from this message.",
        )

    try:
        pr = _parse_single_pr_ref(args=args, rendered_content=context.rendered_content)
    except AssignmentCommandParseError as exc:
        return CommandResult(
            content=f"Could not parse `close-pr` command: {exc.message}",
        )

    user = User.objects.filter(zulip_user_id=context.sender_id).only("id", "github_login").first()
    if user is None:
        return CommandResult(
            content=("No GitHub account is linked to your Zulip profile. Use the `prefs` command to register."),
        )

    github_login = (user.github_login or "").strip()
    if not github_login:
        return CommandResult(
            content="Your Queueboard profile does not have a GitHub login set.",
        )

    result = check_close_pr_permission(
        github_login=github_login,
        owner=pr.owner,
        repo=pr.repo,
        number=pr.number,
    )

    if result.outcome == PermissionOutcome.TOKEN_UNAVAILABLE:
        return CommandResult(
            content=(f"GitHub App token for `close_pr` is not available for `{pr.owner}/{pr.repo}`. Contact an administrator."),
        )

    if result.outcome == PermissionOutcome.GITHUB_ERROR:
        return CommandResult(
            content=(f"Could not fetch PR details for `{pr.owner}/{pr.repo}#{pr.number}` from GitHub. Please try again."),
        )

    if result.outcome == PermissionOutcome.PR_NOT_OPEN:
        pr_ref = f"`{pr.owner}/{pr.repo}#{pr.number}`"
        title_suffix = f' ("{result.pr_title}")' if result.pr_title else ""
        return CommandResult(
            content=f"Pull request {pr_ref}{title_suffix} is not open.",
        )

    if result.outcome == PermissionOutcome.NOT_PERMITTED:
        return CommandResult(
            content=(
                f"Your GitHub account (`{github_login}`) does not have permission "
                f"to close `{pr.owner}/{pr.repo}#{pr.number}`. "
                "Only the PR author or a collaborator with write/admin access may close it."
            ),
        )

    # PERMITTED — build the confirmation link, send it as a private DM, and
    # acknowledge the original message with a reaction. The link is sensitive
    # (single-use, time-limited action token), so it must be delivered privately
    # regardless of whether the command was invoked in a stream or a DM.
    # Zulip outgoing webhook responses always go back to the triggering
    # conversation, so private delivery requires a proactive send_direct_message
    # call; returning the content via CommandResult would expose it in a stream.
    link = build_close_pr_link(
        claims=ClosePRLinkClaims(
            zulip_user_id=context.sender_id,
            github_login=github_login,
            pr_owner=pr.owner,
            pr_repo=pr.repo,
            pr_number=pr.number,
        )
    )
    ttl_seconds = int(getattr(settings, "ZULIP_CLOSE_PR_TOKEN_TTL_SECONDS", 1800))
    expires_at = timezone.now() + timedelta(seconds=ttl_seconds)
    expires_unix = int(expires_at.timestamp())
    pr_ref = f"`{pr.owner}/{pr.repo}#{pr.number}`"
    title_suffix = f' ("{result.pr_title}")' if result.pr_title else ""
    dm_content = (
        f"Use this private link to [confirm closing PR {pr_ref}{title_suffix}]({link}). "
        f"It expires at <time:{expires_unix}>. "
        "The close will be attributed to the bot, not your personal GitHub account."
    )
    try:
        client = ZulipClient()
        if context.message_id is not None:
            try:
                client.add_reaction(message_id=context.message_id, emoji_name="eyes")
            except ZulipApiError:
                logger.warning("close_pr_reaction_failed", extra={"message_id": context.message_id})
        client.send_direct_message(to=[context.sender_id], content=dm_content)
    except ZulipApiError as exc:
        logger.exception("close_pr_dm_failed")
        return CommandResult(content=f"Failed to send private confirmation link: {exc.message}")
    return CommandResult(response_not_required=True)
