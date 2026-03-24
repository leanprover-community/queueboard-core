from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from analyzer.services.pr_info import DependencyInfo, PRQueueInfo, get_pr_queue_info
from analyzer.services.reviewer_attention_format import format_compact_duration, format_since_timestamp
from core.models import User
from zulip_bot.commands import CommandContext, CommandResult, ResponseMode, register_command
from zulip_bot.services.zulip_client import ZulipApiError, ZulipClient

logger = logging.getLogger(__name__)

MAX_PRS = 10

# Matches href="https://github.com/<owner>/<repo>/pull/<number>" in Zulip rendered HTML.
_HREF_RE = re.compile(r'href="https://github\.com/([^/"]+)/([^/"]+)/pull/(\d+)"')
# Fallback for plain-text URL scanning.
_URL_RE = re.compile(r"https://github\.com/([^/\s\"]+)/([^/\s\"]+)/pull/(\d+)")


@register_command(
    name="pr-info",
    description="Show queue info for one or more GitHub PR links (up to 10).",
    response_mode=ResponseMode.STREAM,
)
def pr_info_command(context: CommandContext, args: str) -> CommandResult:
    refs = _parse_pr_refs(context, args)
    if not refs:
        return CommandResult(
            content=("No GitHub PR links found. Usage: `pr-info https://github.com/owner/repo/pull/123`"),
            response_mode=ResponseMode.STREAM,
        )

    client = ZulipClient()

    # React immediately so the user knows we're working.
    if context.message_id is not None:
        try:
            client.add_reaction(message_id=context.message_id, emoji_name="eyes")
        except ZulipApiError:
            logger.warning("pr_info_reaction_failed", extra={"message_id": context.message_id})

    # Fetch info for each PR.
    now = datetime.now(timezone.utc)
    results: list[tuple[tuple[str, str, int], PRQueueInfo | None]] = [
        (ref, get_pr_queue_info(ref[0], ref[1], ref[2])) for ref in refs
    ]

    # Batch-resolve GitHub logins to Zulip silent mentions.
    all_logins: set[str] = set()
    for _, info in results:
        if info is not None:
            if info.author_login:
                all_logins.add(info.author_login)
            all_logins.update(info.assignee_logins)
    mention_map = _build_mention_map(all_logins)

    # Send one message per PR.
    for (owner, repo, number), info in results:
        if info is None:
            url = f"https://github.com/{owner}/{repo}/pull/{number}"
            content = f"[{owner}/{repo}#{number}]({url}): PR not found in our database."
        else:
            content = _format_pr_info(info, mention_map, now)
        _send_reply(client, context, content)

    return CommandResult(content="", response_mode=ResponseMode.STREAM, response_not_required=True)


# ---------------------------------------------------------------------------
# PR link parsing
# ---------------------------------------------------------------------------


def _parse_pr_refs(context: CommandContext, args: str) -> list[tuple[str, str, int]]:
    """Extract up to MAX_PRS unique (owner, repo, number) tuples.

    Prefers hrefs from rendered_content (captures both plain URLs and Markdown
    links as Zulip renders them). Falls back to a plain-text scan of args.
    """
    seen: dict[tuple[str, str, int], None] = {}

    source = context.rendered_content or ""
    pattern = _HREF_RE
    if not source:
        source = args
        pattern = _URL_RE

    for match in pattern.finditer(source):
        owner, repo, num_str = match.group(1), match.group(2), match.group(3)
        ref = (owner, repo, int(num_str))
        seen[ref] = None
        if len(seen) >= MAX_PRS:
            break

    return list(seen)


# ---------------------------------------------------------------------------
# Mention resolution
# ---------------------------------------------------------------------------


def _build_mention_map(logins: set[str]) -> dict[str, str]:
    """Return {github_login: zulip_silent_mention} for known users."""
    if not logins:
        return {}
    result: dict[str, str] = {}
    for user in User.objects.filter(github_login__in=logins).only("github_login", "zulip_user_id", "zulip_full_name"):
        if user.zulip_user_id and user.zulip_full_name:
            result[user.github_login] = f"@_**{user.zulip_full_name}|{user.zulip_user_id}**"
    return result


def _mention(login: str | None, mention_map: dict[str, str]) -> str:
    if not login:
        return "—"
    return mention_map.get(login) or f"`{login}`"


def _mentions(logins: list[str], mention_map: dict[str, str]) -> str:
    if not logins:
        return "—"
    return ", ".join(_mention(login, mention_map) for login in logins)


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------


def _ci_emoji(ci_status: str, ci_requires_success: bool) -> str:
    if ci_status == "pass":
        return ":check:"
    if ci_status == "fail":
        return ":cross_mark:"
    if ci_status == "running":
        return ":yellow:"
    # missing — pass or fail depending on whether CI is required
    base = ":cross_mark:" if ci_requires_success else ":check:"
    return f"{base} (missing)"


def _dep_label(dep: DependencyInfo) -> str:
    url = f"https://github.com/{dep.owner}/{dep.repo}/pull/{dep.number}"
    link = f"[{dep.owner}/{dep.repo}#{dep.number}]({url})"
    if dep.state == "merged":
        return f"{link} [merged]"
    if dep.state == "closed":
        return f"{link} [closed]"
    if dep.is_draft:
        return f"{link} [draft]"
    return link


def _format_pr_info(info: PRQueueInfo, mention_map: dict[str, str], now: datetime) -> str:
    lines: list[str] = []

    # Header: linked PR ref + title + state tag.
    title_link = f"[{info.owner}/{info.repo}#{info.number}]({info.url})"
    state_tag = ""
    if info.state == "merged":
        state_tag = " [merged]"
    elif info.state == "closed":
        state_tag = " [closed]"
    elif info.is_draft:
        state_tag = " [draft]"
    lines.append(f"**{title_link}: {info.title}**{state_tag}")

    # Author + timestamps.
    author_str = _mention(info.author_login, mention_map)
    created_str = format_since_timestamp(info.created_at, now=now)
    updated_str = format_since_timestamp(info.updated_at, now=now)
    lines.append(f"By {author_str}")
    lines.append(f"Created: {created_str}")
    lines.append(f"Updated: {updated_str}")

    # Data freshness line.
    if info.snapshot_generated_at is not None:
        freshness = format_since_timestamp(info.snapshot_generated_at, now=now)
        stale_tag = " ⚠️ stale" if info.snapshot_is_stale else ""
        lines.append(f"Data as of {freshness}{stale_tag}")
    else:
        lines.append("Data from database (no snapshot available)")

    lines.append("")

    # Queue status.
    if info.on_queue:
        since_str = format_since_timestamp(info.queue_since, now=now) if info.queue_since else "unknown"
        total_str = format_compact_duration(info.total_queue_seconds) if info.total_queue_seconds is not None else "unknown"
        lines.append(f"**On queue** since {since_str}  ·  Total queue time: {total_str}")
    elif info.state in ("closed", "merged"):
        total_str = format_compact_duration(info.total_queue_seconds) if info.total_queue_seconds else None
        queue_note = f"  ·  Total queue time: {total_str}" if total_str else ""
        lines.append(f"**Not on queue** ({info.state}){queue_note}")
    else:
        reasons = ", ".join(info.off_queue_reasons) if info.off_queue_reasons else "unknown reason"
        lines.append(f"**Not on queue** — {reasons}")

    # CI + assignees.
    ci_display = _ci_emoji(info.ci_status, info.ci_requires_success)
    assignees_str = _mentions(info.assignee_logins, mention_map)
    lines.append(f"CI: {ci_display}  ·  Assignees: {assignees_str}")

    # Labels.
    if info.labels:
        lines.append("Labels: " + " ".join(f"`{lbl}`" for lbl in info.labels))

    # Dependencies.
    if info.dependencies:
        lines.append("Deps: " + ", ".join(_dep_label(dep) for dep in info.dependencies))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


def _send_reply(client: ZulipClient, context: CommandContext, content: str) -> None:
    try:
        if context.stream_id is not None and context.topic:
            client.send_stream_message(stream=context.stream_id, topic=context.topic, content=content)
        elif context.sender_id is not None:
            client.send_direct_message(to=[context.sender_id], content=content)
    except ZulipApiError:
        logger.exception("pr_info_send_failed")
