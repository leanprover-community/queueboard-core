"""``suggest-prs`` — reviewer-initiated "what should I review?" (design doc 053).

Renders ``analyzer.services.assignment_suggestions`` output; eligibility is never re-derived here.
Replies **in place** (like ``console``, unlike ``assigned-prs``): the content is not sensitive, and
the natural next step — ``assign #12345`` — is itself an in-place command, so splitting one flow
across a DM and a channel would be worse than either. The reply shows the shorter
``ANALYZER_ASSIGNMENT_SUGGESTIONS_ZULIP_LIMIT`` list; a token-less console link (with the request's
repo and labels in the query string, so "more suggestions" means more of the *same* question)
carries the rest. Claiming reuses the existing ``assign`` command — no new mutation surface.
"""

from __future__ import annotations

from urllib.parse import urlencode

from django.conf import settings
from django.urls import reverse

from analyzer.services.assignment_suggestions import (
    STATUS_NO_LABELS,
    STATUS_NO_SNAPSHOT,
    STATUS_NOT_A_REVIEWER,
    SuggestionResult,
    format_skip_summary,
    suggest_prs_for_reviewer,
)
from analyzer.services.reviewer_load import format_load_line
from core.models import Repository, ReviewerPreference, User
from core.services.site_urls import build_site_url
from core.utils.zulip_time import format_global_time
from zulip_bot.commands import CommandContext, CommandResult, register_command
from zulip_bot.services.zulip_client import MAX_MESSAGE_CHARS, ZulipApiError, ZulipClient, split_message_chunks


@register_command(
    name="suggest-prs",
    description="Suggest open PRs you could review. Syntax: suggest-prs [<owner/repo>] [<label> ...].",
    aliases=("next-pr", "suggest-pr"),
)
def suggest_prs_command(context: CommandContext, args: str) -> CommandResult:
    if not bool(getattr(settings, "ANALYZER_ASSIGNMENT_SUGGESTIONS_ENABLED", False)):
        return CommandResult(content="On-demand suggestions are not enabled yet.")
    if context.sender_id is None:
        return CommandResult(content="Could not determine your Zulip identity from this message.")
    user = User.objects.filter(zulip_user_id=context.sender_id).only("id", "github_login").first()
    if user is None or not user.github_login:
        return CommandResult(
            content="No reviewer profile is linked to your Zulip account yet. Run `prefs` to start registration."
        )

    repo_arg, labels = _parse_args(args)
    if repo_arg is not None:
        owner, _, name = repo_arg.partition("/")
        repo = Repository.objects.filter(owner__iexact=owner, name__iexact=name).first()
        if repo is None:
            return CommandResult(content=f"Unknown repository `{repo_arg}`.")
        repos = [repo]
    else:
        repos = [
            pref.repository
            for pref in ReviewerPreference.objects.filter(user=user)
            .select_related("repository")
            .order_by("repository__owner", "repository__name")
        ]
        if not repos:
            return CommandResult(content="You do not currently have any reviewer preferences configured.")

    limit = int(getattr(settings, "ANALYZER_ASSIGNMENT_SUGGESTIONS_ZULIP_LIMIT", 5))
    sections = [
        _render_repo_section(repo, user.github_login, labels=labels, limit=limit, show_heading=len(repos) > 1) for repo in repos
    ]
    content = "\n\n".join(sections)

    # Belt-and-braces (the measured reply is far inside Zulip's cap): if the reply somehow exceeds
    # one message, send the chunks proactively to the same conversation instead of truncating.
    chunks = split_message_chunks(content=content, max_chars=MAX_MESSAGE_CHARS)
    if len(chunks) == 1:
        return CommandResult(content=content)
    try:
        client = ZulipClient()
        for chunk in chunks:
            if context.stream_id is not None and context.topic:
                client.send_stream_message(stream=context.stream_id, topic=context.topic, content=chunk)
            else:
                client.send_direct_message(to=[context.sender_id], content=chunk)
    except ZulipApiError as exc:
        return CommandResult(content=f"Failed to send suggestions via Zulip API: {exc.message}")
    return CommandResult(response_not_required=True)


def _parse_args(args: str) -> tuple[str | None, list[str]]:
    """Split ``[<owner/repo>] [<label> ...]``: a first token containing ``/`` is the repo."""
    tokens = [token.strip().strip(",") for token in (args or "").split()]
    tokens = [token for token in tokens if token]
    repo_arg: str | None = None
    if tokens and "/" in tokens[0]:
        repo_arg = tokens[0]
        tokens = tokens[1:]
    return repo_arg, tokens


def _console_more_url(result: SuggestionResult, labels: list[str]) -> str:
    """Token-less console link carrying the request shape (repo + labels) in the query string.

    No signed link is needed (cf. design doc 052): suggestions are reviewer-only, which is already
    the console's admission rule, and the params only pre-fill the form — the login always comes
    from the console session, never from the request.
    """
    params: dict[str, str] = {"repo": str(result.repository_id)}
    if labels:
        params["labels"] = ",".join(labels)
    return f"{build_site_url(reverse('console:suggestions'))}?{urlencode(params)}"


def _render_repo_section(repo: Repository, reviewer_login: str, *, labels: list[str], limit: int, show_heading: bool) -> str:
    repo_label = f"{repo.owner}/{repo.name}"
    result = suggest_prs_for_reviewer(repo, reviewer_login, labels=labels or None, limit=limit)

    lines: list[str] = []
    if show_heading:
        lines.append(f"## {repo_label}")

    if result.status == STATUS_NO_SNAPSHOT:
        lines.append(f"No queue snapshot is available for {repo_label} right now — try again in a few minutes.")
        return "\n".join(lines)
    if result.status == STATUS_NOT_A_REVIEWER:
        lines.append(f"You are not registered as a reviewer for {repo_label}.")
        return "\n".join(lines)

    if result.unknown_labels:
        ignored = ", ".join(f"`{label}`" for label in result.unknown_labels)
        lines.append(f"Ignored (not topic labels in {repo_label}): {ignored}.")
    if result.status == STATUS_NO_LABELS:
        lines.append(f"You have no preferred labels for {repo_label} — name some, e.g. `suggest-prs {repo_label} t-algebra`.")
        return "\n".join(lines)

    if result.load is not None:
        lines.append(format_load_line(result.load))

    if not result.suggestions:
        summary = format_skip_summary(result.skipped)
        tail = f" — skipped: {summary}" if summary else ""
        lines.append(f"No eligible PRs right now{tail}.")
        return "\n".join(lines)

    for pr in result.suggestions:
        label_str = " ".join(f"`{label}`" for label in pr.topic_labels)
        line = f"- [#{pr.pr_number}]({pr.url}): {pr.title}"
        if label_str:
            line += f" — {label_str}"
        lines.append(line)

    # Footer. The wording stays indefinite ("more suggestions", never "the next N"): the prefix
    # property only holds within one snapshot generation, and the snapshot refreshes.
    footer_bits = [f"Take one with `assign #{result.suggestions[0].pr_number}`."]
    footer_bits.append(f"[More suggestions on the console]({_console_more_url(result, labels)}).")
    if result.snapshot_generated_at is not None:
        footer_bits.append(f"Snapshot from {format_global_time(result.snapshot_generated_at)}.")
    lines.append(" ".join(footer_bits))
    return "\n".join(lines)
