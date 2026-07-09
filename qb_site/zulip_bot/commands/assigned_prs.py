from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from analyzer.services.ci_evaluation import batch_ci_statuses_for_repo
from analyzer.services.pr_info import off_queue_reasons_from_labels
from analyzer.services.queue_rules import QueueRules, load_rules_for_repo
from analyzer.services.reviewer_attention_format import (
    format_since_timestamp,
    render_consecutive_queue_time_since_assignment_line,
    render_total_queue_time_line,
    sort_by_assignment_recency,
    sort_by_queue_age,
)
from analyzer.services.reviewer_attention import ReviewerAttentionItem, ReviewerAttentionReport, build_reviewer_attention_reports
from analyzer.services.reviewer_load import ReviewerLoad, build_reviewer_loads, format_load_line, normalize_login
from core.models import Repository, ReviewerPreference, User
from core.utils.zulip_time import format_global_time
from syncer.models import PRLabel, PullRequest
from zulip_bot.commands import CommandContext, CommandResult, register_command
from zulip_bot.services.zulip_client import MAX_MESSAGE_CHARS, ZulipApiError, ZulipClient, split_message_chunks

_NO_REQUIRED_FAILURES = "no_required_failures"


@register_command(
    name="assigned-prs",
    description="Show your assigned open PRs with queue-time status.",
)
def assigned_prs_command(context: CommandContext, args: str) -> CommandResult:
    del args

    if context.sender_id is None:
        return CommandResult(content="Could not determine your Zulip identity from this message.")

    user = User.objects.filter(zulip_user_id=context.sender_id).only("id", "github_login").first()
    if user is None:
        return CommandResult(
            content="No reviewer profile is linked to your Zulip account yet. Run `prefs` to start registration."
        )

    prefs = list(
        ReviewerPreference.objects.filter(user_id=user.id)
        .select_related("repository")
        .order_by("repository__owner", "repository__name")
    )
    if not prefs:
        return CommandResult(content="You do not currently have any reviewer preferences configured.")

    reviewer_login_norm = normalize_login(user.github_login)
    reports_by_repo_id: dict[int, ReviewerAttentionReport] = {}
    repo_labels_by_repo_id: dict[int, str] = {}
    load_by_repo_id: dict[int, ReviewerLoad] = {}
    for pref in prefs:
        repo_labels_by_repo_id[int(pref.repository_id)] = f"{pref.repository.owner}/{pref.repository.name}"
        reports = build_reviewer_attention_reports(repository=pref.repository)
        report = next((entry for entry in reports if entry.reviewer_user_id == user.id), None)
        if report is not None:
            reports_by_repo_id[int(pref.repository_id)] = report
        # Reviewer load as of the latest queue snapshot; absent snapshot -> no load line.
        load = build_reviewer_loads(pref.repository).get(reviewer_login_norm)
        if load is not None:
            load_by_repo_id[int(pref.repository_id)] = load

    # Batch-fetch display extras (author, CI, labels, timestamps) per repo.
    extras_by_repo_and_pr: dict[tuple[int, int], _PrExtras] = {}
    all_author_logins: set[str] = set()
    for pref in prefs:
        repo_id = int(pref.repository_id)
        report = reports_by_repo_id.get(repo_id)
        if report is None:
            continue
        pr_numbers = [int(item.pr_number) for item in report.items]
        extras = _batch_fetch_pr_extras(pref.repository, pr_numbers)
        for number, ex in extras.items():
            extras_by_repo_and_pr[(repo_id, number)] = ex
            if ex.author_login:
                all_author_logins.add(ex.author_login)

    mention_map = _build_mention_map(all_author_logins)

    content = _render_assigned_prs_report(
        reviewer_login=user.github_login or f"user-{user.id}",
        reports=[
            (
                repo_labels_by_repo_id[int(pref.repository_id)],
                reports_by_repo_id[int(pref.repository_id)],
                {
                    number: extras_by_repo_and_pr[(int(pref.repository_id), number)]
                    for number in [int(item.pr_number) for item in reports_by_repo_id[int(pref.repository_id)].items]
                    if (int(pref.repository_id), number) in extras_by_repo_and_pr
                },
            )
            for pref in prefs
            if int(pref.repository_id) in reports_by_repo_id
        ],
        mention_map=mention_map,
        load_by_repo_id=load_by_repo_id,
    )
    chunks = split_message_chunks(content=content, max_chars=MAX_MESSAGE_CHARS)

    try:
        client = ZulipClient()
        for chunk in chunks:
            client.send_direct_message(to=[context.sender_id], content=chunk)
    except ZulipApiError as exc:
        return CommandResult(content=f"Failed to send assigned PR report via Zulip API: {exc.message}")

    return CommandResult(response_not_required=True)


# ---------------------------------------------------------------------------
# PR display extras
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _PrExtras:
    author_login: str | None
    is_draft: bool
    ci_status: str
    ci_requires_success: bool
    labels: list[str]
    created_at: datetime | None
    updated_at: datetime | None
    rules: QueueRules


def _batch_fetch_pr_extras(repository: Repository, pr_numbers: list[int]) -> dict[int, _PrExtras]:
    """Batch-fetch display extras for a set of open assigned PRs in one repo."""
    if not pr_numbers:
        return {}

    rules = load_rules_for_repo(repository)
    ci_requires_success = rules.require_ci_success and rules.ci_gating_mode != _NO_REQUIRED_FAILURES

    prs = list(
        PullRequest.objects.filter(repository=repository, number__in=pr_numbers)
        .select_related("author")
        .only("id", "number", "is_draft", "head_sha", "gh_created_at", "gh_updated_at", "author")
    )
    pr_by_number = {pr.number: pr for pr in prs}

    ci_statuses = batch_ci_statuses_for_repo(prs, rules, repository)

    label_map: dict[int, list[str]] = {}
    for row in PRLabel.objects.filter(
        pull_request__repository=repository,
        pull_request__number__in=pr_numbers,
    ).values("pull_request__number", "label_def__name"):
        label_map.setdefault(row["pull_request__number"], []).append(row["label_def__name"])

    result: dict[int, _PrExtras] = {}
    for number in pr_numbers:
        pr = pr_by_number.get(number)
        result[number] = _PrExtras(
            author_login=pr.author.github_login if pr and pr.author else None,
            is_draft=pr.is_draft if pr else False,
            ci_status=ci_statuses.get(number, "missing"),
            ci_requires_success=ci_requires_success,
            labels=sorted(label_map.get(number, [])),
            created_at=_ensure_utc(pr.gh_created_at) if pr else None,
            updated_at=_ensure_utc(pr.gh_updated_at) if pr else None,
            rules=rules,
        )
    return result


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def _render_assigned_prs_report(
    *,
    reviewer_login: str,
    reports: list[tuple[str, ReviewerAttentionReport, dict[int, _PrExtras]]],
    mention_map: dict[str, str],
    load_by_repo_id: dict[int, ReviewerLoad] | None = None,
) -> str:
    load_by_repo_id = load_by_repo_id or {}
    lines: list[str] = []
    lines.append(f"### Assigned PRs report for `{reviewer_login}`")
    lines.append("")

    if not reports:
        lines.append("No assigned PR data is available for your configured repositories.")
        return "\n".join(lines)

    lines.append(f"Generated at {format_global_time(_now_utc_unix())}.")

    # Cross-repo heads-up, surfaced only when it's actionable (at capacity somewhere). Counted over
    # the repos actually shown below (those with a load figure available).
    shown_loads = [load_by_repo_id[report.repository_id] for _, report, _ in reports if report.repository_id in load_by_repo_id]
    at_capacity_loads = [load for load in shown_loads if load.at_capacity]
    if at_capacity_loads:
        lines.append(f"⚠ At capacity in {len(at_capacity_loads)} of {len(shown_loads)} repos.")

    any_assigned = False
    for repo_label, report, extras_by_pr in reports:
        lines.append("")
        lines.append(f"## {repo_label}")
        lines.append(
            f"Thresholds: stale nudge `{report.stale_nudge_days}` days, auto-unassign `{report.auto_unassign_days}` days."
        )
        load = load_by_repo_id.get(report.repository_id)
        if load is not None:
            # Weighted load vs capacity; the raw PR count is omitted here since the group headers
            # (On Queue / Maintainer Merged / Not On Queue) already sum to it.
            lines.append(format_load_line(load))

        if report.warnings:
            lines.append("Warnings:")
            for warning in report.warnings:
                lines.append(f"- {warning}")

        if not report.items:
            lines.append("- No currently assigned open PRs.")
            continue

        any_assigned = True
        owner, _, repo_name = repo_label.partition("/")
        maintainer_merge_pr_numbers = _maintainer_merge_pr_numbers(report=report)
        lines.extend(
            _render_items(
                items=report.items,
                maintainer_merge_pr_numbers=maintainer_merge_pr_numbers,
                stale_nudge_days=report.stale_nudge_days,
                auto_unassign_days=report.auto_unassign_days,
                extras_by_pr=extras_by_pr,
                mention_map=mention_map,
                repo_owner=owner,
                repo_name=repo_name,
            )
        )

    if not any_assigned:
        lines.append("")
        lines.append("No currently assigned open PRs across your configured repositories.")

    return "\n".join(lines)


def _render_items(
    *,
    items: tuple[ReviewerAttentionItem, ...],
    maintainer_merge_pr_numbers: set[int],
    stale_nudge_days: int,
    auto_unassign_days: int,
    extras_by_pr: dict[int, _PrExtras],
    mention_map: dict[str, str],
    repo_owner: str,
    repo_name: str,
) -> list[str]:
    lines: list[str] = []
    maintainer_merged_items = tuple(
        sort_by_queue_age([item for item in items if item.is_on_queue and int(item.pr_number) in maintainer_merge_pr_numbers])
    )
    on_queue_items = tuple(
        sort_by_queue_age([item for item in items if item.is_on_queue and int(item.pr_number) not in maintainer_merge_pr_numbers])
    )
    off_queue_items = tuple(sort_by_assignment_recency([item for item in items if not item.is_on_queue]))

    shared = dict(
        extras_by_pr=extras_by_pr,
        mention_map=mention_map,
        repo_owner=repo_owner,
        repo_name=repo_name,
        stale_nudge_days=stale_nudge_days,
        auto_unassign_days=auto_unassign_days,
    )
    lines.extend(
        _render_item_group(title=f"On Queue ({len(on_queue_items)})", items=on_queue_items, include_consecutive=True, **shared)
    )
    lines.extend(
        _render_item_group(
            title=f"Maintainer Merged ({len(maintainer_merged_items)})",
            items=maintainer_merged_items,
            include_consecutive=True,
            **shared,
        )
    )
    lines.extend(
        _render_item_group(
            title=f"Not On Queue ({len(off_queue_items)})", items=off_queue_items, include_consecutive=False, **shared
        )
    )
    return lines


def _render_item_group(
    *,
    title: str,
    items: tuple[ReviewerAttentionItem, ...],
    include_consecutive: bool,
    extras_by_pr: dict[int, _PrExtras],
    mention_map: dict[str, str],
    repo_owner: str,
    repo_name: str,
    stale_nudge_days: int,
    auto_unassign_days: int,
) -> list[str]:
    lines: list[str] = [f"```spoiler {title}"]
    if not items:
        lines.append("- None.")
        lines.append("```")
        return lines

    for item in items:
        pr_number = int(item.pr_number)
        extras = extras_by_pr.get(pr_number)
        url = f"https://github.com/{repo_owner}/{repo_name}/pull/{pr_number}"

        # Header line: linked PR number + title + draft tag.
        draft_tag = " [draft]" if (extras and extras.is_draft) else ""
        lines.append(f"- [#{pr_number}]({url}): {item.pr_title}{draft_tag}")

        # Author + CI line.
        if extras is not None:
            author_str = _mention(extras.author_login, mention_map)
            ci_str = _ci_emoji(extras.ci_status, extras.ci_requires_success)
            lines.append(f"  - By {author_str} · CI: {ci_str}")

        # Timestamps.
        if extras is not None:
            created_str = format_since_timestamp(extras.created_at)
            updated_str = format_since_timestamp(extras.updated_at)
            lines.append(f"  - Created: {created_str} · Updated: {updated_str}")

        # Labels.
        if extras and extras.labels:
            label_str = " ".join(f"`{lbl}`" for lbl in extras.labels)
            lines.append(f"  - Labels: {label_str}")

        # Queue timing / off-queue reasons.
        if include_consecutive:
            lines.append(f"  - {render_consecutive_queue_time_since_assignment_line(item)}")
        elif extras is not None:
            # Off-queue: show why (all PRs in this report are open).
            label_set = {lbl.lower() for lbl in extras.labels}
            reasons = off_queue_reasons_from_labels(label_set, extras.rules, extras.ci_status, extras.is_draft)
            if reasons:
                lines.append(f"  - Off queue: {', '.join(reasons)}")

        lines.append(f"  - Assigned: {format_since_timestamp(item.last_assigned_at)}")
        lines.append(f"  - {render_total_queue_time_line(item)}")

        if item.missing_assignment_timestamp:
            lines.append("  - Note: missing assignment timestamp; policy flags suppressed")

        friendly_status = _friendly_status_line(
            item=item, stale_nudge_days=stale_nudge_days, auto_unassign_days=auto_unassign_days
        )
        if friendly_status is not None:
            lines.append(f"  - {friendly_status}")

    lines.append("```")
    return lines


def _friendly_status_line(
    *,
    item: ReviewerAttentionItem,
    stale_nudge_days: int,
    auto_unassign_days: int,
) -> str | None:
    if item.needs_auto_unassign:
        return (
            "This PR has been on the queue for >= "
            f"{auto_unassign_days} consecutive days and you will be automatically unassigned soon."
        )
    if item.needs_nudge:
        return f"This PR has been on the queue for >= {stale_nudge_days} consecutive days since you were assigned."
    return None


# ---------------------------------------------------------------------------
# Mention helpers
# ---------------------------------------------------------------------------


def _build_mention_map(logins: set[str]) -> dict[str, str]:
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


# ---------------------------------------------------------------------------
# CI emoji helper
# ---------------------------------------------------------------------------


def _ci_emoji(ci_status: str, ci_requires_success: bool) -> str:
    if ci_status in ("pass", "fail-inessential"):
        return ":check:"
    if ci_status == "fail":
        return ":cross_mark:"
    if ci_status == "running":
        return ":yellow:"
    # missing
    base = ":cross_mark:" if ci_requires_success else ":check:"
    return f"{base} (missing)"


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _now_utc_unix() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _ensure_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


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
