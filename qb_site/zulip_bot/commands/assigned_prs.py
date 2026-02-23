from __future__ import annotations

from datetime import datetime, timedelta
from datetime import timezone

from analyzer.services.reviewer_attention import ReviewerAttentionItem, ReviewerAttentionReport, build_reviewer_attention_reports
from core.models import ReviewerPreference, User
from syncer.models import PRLabel
from zulip_bot.commands import CommandContext, CommandResult, ResponseMode, register_command
from zulip_bot.services.zulip_client import ZulipApiError, ZulipClient

MAX_MESSAGE_CHARS = 9000


@register_command(
    name="assigned_prs",
    description="Show your assigned open PRs with queue-time status.",
    response_mode=ResponseMode.PRIVATE,
)
def assigned_prs_command(context: CommandContext, args: str) -> CommandResult:
    del args

    if context.sender_id is None:
        return CommandResult(
            content="Could not determine your Zulip identity from this message.",
            response_mode=ResponseMode.PRIVATE,
        )

    user = User.objects.filter(zulip_user_id=context.sender_id).only("id", "github_login").first()
    if user is None:
        return CommandResult(
            content="No reviewer profile is linked to your Zulip account yet. Run `prefs` to start registration.",
            response_mode=ResponseMode.PRIVATE,
        )

    prefs = list(
        ReviewerPreference.objects.filter(user_id=user.id)
        .select_related("repository")
        .order_by("repository__owner", "repository__name")
    )
    if not prefs:
        return CommandResult(
            content="You do not currently have any reviewer preferences configured.",
            response_mode=ResponseMode.PRIVATE,
        )

    reports_by_repo_id: dict[int, ReviewerAttentionReport] = {}
    repo_labels_by_repo_id: dict[int, str] = {}
    for pref in prefs:
        repo_labels_by_repo_id[int(pref.repository_id)] = f"{pref.repository.owner}/{pref.repository.name}"
        reports = build_reviewer_attention_reports(repository=pref.repository)
        report = next((entry for entry in reports if entry.reviewer_user_id == user.id), None)
        if report is not None:
            reports_by_repo_id[int(pref.repository_id)] = report

    content = _render_assigned_prs_report(
        reviewer_login=user.github_login or f"user-{user.id}",
        reports=[
            (repo_labels_by_repo_id[int(pref.repository_id)], reports_by_repo_id[int(pref.repository_id)])
            for pref in prefs
            if int(pref.repository_id) in reports_by_repo_id
        ],
    )
    chunks = _split_message_chunks(content=content, max_chars=MAX_MESSAGE_CHARS)

    try:
        client = ZulipClient()
        for chunk in chunks:
            client.send_direct_message(to=[context.sender_id], content=chunk)
    except ZulipApiError as exc:
        return CommandResult(
            content=f"Failed to send assigned PR report via Zulip API: {exc.message}",
            response_mode=ResponseMode.PRIVATE,
        )

    return CommandResult(content="", response_mode=ResponseMode.PRIVATE, response_not_required=True)


def _render_assigned_prs_report(*, reviewer_login: str, reports: list[tuple[str, ReviewerAttentionReport]]) -> str:
    lines: list[str] = []
    lines.append(f"### Assigned PRs report for `{reviewer_login}`")
    lines.append("")

    if not reports:
        lines.append("No assigned PR data is available for your configured repositories.")
        return "\n".join(lines)

    now_unix = _now_utc_unix()
    lines.append(f"Generated at <time:{now_unix}>.")

    any_assigned = False
    for repo_label, report in reports:
        lines.append("")
        lines.append(f"## {repo_label}")
        lines.append(
            f"Thresholds: stale nudge `{report.stale_nudge_days}` days, auto-unassign `{report.auto_unassign_days}` days."
        )

        if report.warnings:
            lines.append("Warnings:")
            for warning in report.warnings:
                lines.append(f"- {warning}")

        if not report.items:
            lines.append("- No currently assigned open PRs.")
            continue

        any_assigned = True
        maintainer_merge_pr_numbers = _maintainer_merge_pr_numbers(report=report)
        lines.extend(_render_items(items=report.items, maintainer_merge_pr_numbers=maintainer_merge_pr_numbers))

    if not any_assigned:
        lines.append("")
        lines.append("No currently assigned open PRs across your configured repositories.")

    return "\n".join(lines)


def _render_items(*, items: tuple[ReviewerAttentionItem, ...], maintainer_merge_pr_numbers: set[int]) -> list[str]:
    lines: list[str] = []
    maintainer_merged_items = tuple(
        sorted(
            (item for item in items if item.is_on_queue and int(item.pr_number) in maintainer_merge_pr_numbers),
            key=lambda entry: (
                -(entry.days_on_queue_since_assignment or -1),
                entry.pr_number,
            ),
        )
    )
    on_queue_items = tuple(
        sorted(
            (item for item in items if item.is_on_queue and int(item.pr_number) not in maintainer_merge_pr_numbers),
            key=lambda entry: (
                -(entry.days_on_queue_since_assignment or -1),
                entry.pr_number,
            ),
        )
    )
    off_queue_items = tuple(sorted((item for item in items if not item.is_on_queue), key=lambda entry: entry.pr_number))

    lines.extend(_render_item_group(title=f"On Queue ({len(on_queue_items)})", items=on_queue_items, include_consecutive=True))
    lines.extend(
        _render_item_group(
            title=f"Maintainer Merged ({len(maintainer_merged_items)})",
            items=maintainer_merged_items,
            include_consecutive=True,
        )
    )
    lines.extend(
        _render_item_group(title=f"Not On Queue ({len(off_queue_items)})", items=off_queue_items, include_consecutive=False)
    )
    return lines


def _render_item_group(*, title: str, items: tuple[ReviewerAttentionItem, ...], include_consecutive: bool) -> list[str]:
    lines: list[str] = [f"```spoiler {title}"]
    if not items:
        lines.append("- None.")
        lines.append("```")
        return lines

    for item in items:
        lines.append(f"- PR #{item.pr_number}: {item.pr_title}")
        if include_consecutive:
            if item.days_on_queue_since_assignment is None:
                lines.append("  - Consecutive queue age since assignment: unavailable")
            else:
                consecutive_seconds = int(item.days_on_queue_since_assignment) * 24 * 60 * 60
                lines.append(f"  - Consecutive queue age since assignment: {_format_duration(consecutive_seconds)}")
        if item.total_queue_days is None:
            lines.append("  - Total queue time: unavailable")
        else:
            lines.append(f"  - Total queue time: {_format_duration(item.total_queue_seconds or 0)}")
        if item.missing_assignment_timestamp:
            lines.append("  - Note: missing assignment timestamp; policy flags suppressed")
        flags: list[str] = []
        if item.needs_nudge:
            flags.append("needs_nudge")
        if item.needs_auto_unassign:
            flags.append("needs_auto_unassign")
        lines.append(f"  - Flags: {', '.join(flags) if flags else 'none'}")
    lines.append("```")
    return lines


def _format_duration(total_seconds: int) -> str:
    if total_seconds < 0:
        total_seconds = 0
    delta = timedelta(seconds=int(total_seconds))
    seconds = int(delta.total_seconds())
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)

    # Readability thresholds:
    # - >= 5 days: day precision
    # - [1 day, 5 days): days + hours
    # - [1 hour, 1 day): hours + minutes
    # - [1 minute, 1 hour): minutes + seconds
    # - < 1 minute: seconds
    if days >= 5:
        return f"{days}d"
    if days >= 1:
        return f"{days}d {hours}h"
    if hours >= 1:
        return f"{hours}h {minutes}m"
    if minutes >= 1:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _split_message_chunks(*, content: str, max_chars: int) -> list[str]:
    if len(content) <= max_chars:
        return [content]

    lines = content.splitlines()
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        line_len = len(line) + 1
        if current and current_len + line_len > max_chars:
            chunks.append("\n".join(current))
            current = [line]
            current_len = line_len
            continue
        if not current and line_len > max_chars:
            # Fallback for an oversized single line.
            start = 0
            while start < len(line):
                end = min(start + max_chars, len(line))
                chunks.append(line[start:end])
                start = end
            current = []
            current_len = 0
            continue
        current.append(line)
        current_len += line_len

    if current:
        chunks.append("\n".join(current))
    return chunks


def _now_utc_unix() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _maintainer_merge_pr_numbers(*, report: ReviewerAttentionReport) -> set[int]:
    pr_numbers = [int(item.pr_number) for item in report.items if item.is_on_queue]
    if not pr_numbers:
        return set()
    return set(
        PRLabel.objects.filter(
            pull_request__repository_id=report.repository_id,
            pull_request__number__in=pr_numbers,
            label_def__name__iexact="maintainer-merge",
        ).values_list("pull_request__number", flat=True)
    )
