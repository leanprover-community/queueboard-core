from __future__ import annotations

from datetime import timezone

from analyzer.services.reviewer_attention import ReviewerAttentionItem, ReviewerAttentionReport, build_reviewer_attention_reports
from core.models import ReviewerPreference, User
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

    now_iso = _now_utc_iso()
    lines.append(f"Generated at `{now_iso}`.")

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
        lines.extend(_render_items(report.items))

    if not any_assigned:
        lines.append("")
        lines.append("No currently assigned open PRs across your configured repositories.")

    return "\n".join(lines)


def _render_items(items: tuple[ReviewerAttentionItem, ...]) -> list[str]:
    lines: list[str] = []
    for item in sorted(items, key=lambda entry: entry.pr_number):
        lines.append(f"- PR #{item.pr_number}: {item.pr_title}")
        lines.append(f"  - On queue now: {'yes' if item.is_on_queue else 'no'}")
        if item.days_on_queue_since_assignment is None:
            lines.append("  - Consecutive queue age since assignment: unavailable")
        else:
            lines.append(f"  - Consecutive queue age since assignment: {item.days_on_queue_since_assignment} day(s)")
        if item.total_queue_days is None:
            lines.append("  - Total queue time: unavailable")
        else:
            lines.append(f"  - Total queue time: {item.total_queue_days} day(s) ({item.total_queue_seconds} seconds)")
        if item.missing_assignment_timestamp:
            lines.append("  - Note: missing assignment timestamp; policy flags suppressed")
        flags: list[str] = []
        if item.needs_nudge:
            flags.append("needs_nudge")
        if item.needs_auto_unassign:
            flags.append("needs_auto_unassign")
        lines.append(f"  - Flags: {', '.join(flags) if flags else 'none'}")
    return lines


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


def _now_utc_iso() -> str:
    from datetime import datetime

    return datetime.now(timezone.utc).isoformat()
