from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from core.models import User
from zulip_bot.commands import CommandContext, CommandResult, ResponseMode, register_command
from zulip_bot.services.assignment_command_parser import AssignmentCommandParseError, _parse_single_issue_or_pr_ref
from zulip_bot.services.close_pr_execution import PermissionOutcome
from zulip_bot.services.label_pr_execution import check_label_pr_permission
from zulip_bot.services.label_pr_links import LabelPRLinkClaims, build_label_pr_link
from zulip_bot.services.zulip_client import ZulipApiError, ZulipClient

logger = logging.getLogger(__name__)


@register_command(
    name="label-pr",
    description="Open a private form to edit labels on a pull request or issue (requires GitHub write access).",
    response_mode=ResponseMode.PRIVATE,
)
def label_pr_command(context: CommandContext, args: str) -> CommandResult:
    if context.sender_id is None:
        return CommandResult(
            content="Could not determine your Zulip identity from this message.",
            response_mode=ResponseMode.PRIVATE,
        )

    try:
        ref = _parse_single_issue_or_pr_ref(args=args, rendered_content=context.rendered_content)
    except AssignmentCommandParseError as exc:
        return CommandResult(
            content=f"Could not parse `label-pr` command: {exc.message}",
            response_mode=ResponseMode.PRIVATE,
        )

    user = User.objects.filter(zulip_user_id=context.sender_id).only("id", "github_login").first()
    if user is None:
        return CommandResult(
            content="No GitHub account is linked to your Zulip profile. Use the `prefs` command to register.",
            response_mode=ResponseMode.PRIVATE,
        )

    github_login = (user.github_login or "").strip()
    if not github_login:
        return CommandResult(
            content="Your Queueboard profile does not have a GitHub login set.",
            response_mode=ResponseMode.PRIVATE,
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
            response_mode=ResponseMode.PRIVATE,
        )

    if result.outcome == PermissionOutcome.GITHUB_ERROR:
        return CommandResult(
            content=f"Could not fetch details for `{ref.owner}/{ref.repo}#{ref.number}` from GitHub. Please try again.",
            response_mode=ResponseMode.PRIVATE,
        )

    if result.outcome == PermissionOutcome.PR_NOT_OPEN:
        ref_str = f"`{ref.owner}/{ref.repo}#{ref.number}`"
        title_suffix = f' ("{result.pr_title}")' if result.pr_title else ""
        return CommandResult(
            content=f"Issue/PR {ref_str}{title_suffix} is not open.",
            response_mode=ResponseMode.PRIVATE,
        )

    if result.outcome == PermissionOutcome.NOT_PERMITTED:
        return CommandResult(
            content=(
                f"Your GitHub account (`{github_login}`) does not have permission "
                f"to label `{ref.owner}/{ref.repo}#{ref.number}`. "
                "Write or admin access is required."
            ),
            response_mode=ResponseMode.PRIVATE,
        )

    # PERMITTED — react to acknowledge, then issue the confirmation link.
    if context.message_id is not None:
        try:
            ZulipClient().add_reaction(message_id=context.message_id, emoji_name="eyes")
        except ZulipApiError:
            logger.warning("label_pr_reaction_failed", extra={"message_id": context.message_id})

    link = build_label_pr_link(
        claims=LabelPRLinkClaims(
            zulip_user_id=context.sender_id,
            github_login=github_login,
            pr_owner=ref.owner,
            pr_repo=ref.repo,
            pr_number=ref.number,
        )
    )
    ttl_seconds = int(getattr(settings, "ZULIP_LABEL_PR_TOKEN_TTL_SECONDS", 1800))
    expires_at = timezone.now() + timedelta(seconds=ttl_seconds)
    expires_unix = int(expires_at.timestamp())
    ref_str = f"`{ref.owner}/{ref.repo}#{ref.number}`"
    title_suffix = f' ("{result.pr_title}")' if result.pr_title else ""
    return CommandResult(
        content=(
            f"Use this private link to [edit labels on {ref_str}{title_suffix}]({link}). "
            f"It expires at <time:{expires_unix}>. "
            "The change will be attributed to the bot, not your personal GitHub account."
        ),
        response_mode=ResponseMode.PRIVATE,
    )
