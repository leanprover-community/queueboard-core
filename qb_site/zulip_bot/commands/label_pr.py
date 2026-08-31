from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from core.models import User
from core.utils.zulip_time import format_global_time
from zulip_bot.commands import CommandContext, CommandResult, register_command
from zulip_bot.services.assignment_command_parser import AssignmentCommandParseError, _parse_single_issue_or_pr_ref
from zulip_bot.services.close_pr_execution import PermissionOutcome
from zulip_bot.services.label_pr_execution import check_label_pr_permission
from zulip_bot.services.pr_action_links import LABEL_PR, PRActionLinkClaims, build_pr_action_link, ttl_seconds
from zulip_bot.services.zulip_client import ZulipApiError, ZulipClient

logger = logging.getLogger(__name__)


@register_command(
    name="label-pr",
    description="Open a private form to edit labels on a pull request or issue (requires GitHub write access).",
)
def label_pr_command(context: CommandContext, args: str) -> CommandResult:
    if context.sender_id is None:
        return CommandResult(
            content="Could not determine your Zulip identity from this message.",
        )

    try:
        ref = _parse_single_issue_or_pr_ref(args=args, rendered_content=context.rendered_content)
    except AssignmentCommandParseError as exc:
        return CommandResult(
            content=f"Could not parse `label-pr` command: {exc.message}",
        )

    user = User.objects.filter(zulip_user_id=context.sender_id).only("id", "github_login").first()
    if user is None:
        return CommandResult(
            content="No GitHub account is linked to your Zulip profile. Use the `prefs` command to register.",
        )

    github_login = (user.github_login or "").strip()
    if not github_login:
        return CommandResult(
            content="Your Queueboard profile does not have a GitHub login set.",
        )

    result = check_label_pr_permission(
        github_login=github_login,
        owner=ref.owner,
        repo=ref.repo,
        number=ref.number,
    )

    if result.outcome == PermissionOutcome.TOKEN_UNAVAILABLE:
        return CommandResult(
            content=f"GitHub App token for `label_pr` is not available for `{ref.owner}/{ref.repo}`. Contact an administrator.",
        )

    if result.outcome == PermissionOutcome.GITHUB_ERROR:
        return CommandResult(
            content=f"Could not fetch details for `{ref.owner}/{ref.repo}#{ref.number}` from GitHub. Please try again.",
        )

    if result.outcome == PermissionOutcome.PR_NOT_OPEN:
        ref_str = f"`{ref.owner}/{ref.repo}#{ref.number}`"
        title_suffix = f' ("{result.pr_title}")' if result.pr_title else ""
        return CommandResult(
            content=f"Issue/PR {ref_str}{title_suffix} is not open.",
        )

    if result.outcome == PermissionOutcome.NOT_PERMITTED:
        return CommandResult(
            content=(
                f"Your GitHub account (`{github_login}`) does not have permission "
                f"to label `{ref.owner}/{ref.repo}#{ref.number}`. "
                "Write or admin access is required."
            ),
        )

    # PERMITTED — build the label-editing link, send it as a private DM, and
    # acknowledge the original message with a reaction. See close_pr.py for the
    # rationale: the link must be delivered via send_direct_message because Zulip
    # webhook responses always go back to the triggering conversation.
    link = build_pr_action_link(
        action=LABEL_PR,
        claims=PRActionLinkClaims(
            zulip_user_id=context.sender_id,
            github_login=github_login,
            pr_owner=ref.owner,
            pr_repo=ref.repo,
            pr_number=ref.number,
        ),
    )
    expires_at = timezone.now() + timedelta(seconds=ttl_seconds(LABEL_PR))
    ref_str = f"`{ref.owner}/{ref.repo}#{ref.number}`"
    title_suffix = f' ("{result.pr_title}")' if result.pr_title else ""
    dm_content = (
        f"Use this private link to [edit labels on {ref_str}{title_suffix}]({link}). "
        f"It expires at {format_global_time(expires_at)}. "
        "The change will be attributed to the bot, not your personal GitHub account."
    )
    try:
        client = ZulipClient()
        if context.message_id is not None:
            try:
                client.add_reaction(message_id=context.message_id, emoji_name="eyes")
            except ZulipApiError:
                logger.warning("label_pr_reaction_failed", extra={"message_id": context.message_id})
        client.send_direct_message(to=[context.sender_id], content=dm_content)
    except ZulipApiError as exc:
        logger.exception("label_pr_dm_failed")
        return CommandResult(content=f"Failed to send private label-editing link: {exc.message}")
    return CommandResult(response_not_required=True)
