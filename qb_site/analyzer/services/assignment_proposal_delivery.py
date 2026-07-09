"""Deliver the per-reviewer assignment-proposal digest DM (design doc 050, Chunk 5).

For each ``confirm``-mode reviewer with pending proposals awaiting their decision, send **one**
Zulip DM listing every such PR with a link to the console (where they accept/decline). The digest
spans repositories so a reviewer never gets one ping per repo.

Dedupe is carried by ``AssignmentProposal.notified_at`` (no separate record model): a reviewer's
digest covers only their ``state=proposed, notified_at IS NULL`` proposals, and ``notified_at`` is
stamped after a successful send. New proposals created next cycle are un-notified again and roll
into the next digest. The stamp is a conditional ``UPDATE ... WHERE state='proposed' AND
notified_at IS NULL`` so a concurrent sweep/accept that already retired or notified a row is never
clobbered.

Reachability is ``core.User.zulip_user_id``; unreachable reviewers never receive proposals in the
first place (Chunk 4 falls them back to auto), but we still skip defensively. ``dry_run`` computes
the would-send set with no DMs and no ``notified_at`` writes.
"""

from __future__ import annotations

import functools
import logging
import operator
from datetime import datetime
from typing import Any, Iterable

from django.db.models import Q
from django.urls import reverse

from analyzer.models import AssignmentProposal
from analyzer.services.reviewer_assignment_engine import _normalize_login
from core.models import Repository, User
from core.services.site_urls import build_site_url
from core.utils.zulip_time import format_global_time
from syncer.models import PullRequest
from zulip_bot.services.zulip_client import MAX_MESSAGE_CHARS, ZulipApiError, ZulipClient, split_message_chunks

log = logging.getLogger(__name__)


def _empty_stats() -> dict[str, int]:
    return {
        "pending_proposals": 0,
        "reviewers": 0,
        "attempted": 0,
        "sent": 0,
        "would_send": 0,
        "failed": 0,
        "skipped_no_user": 0,
        "skipped_no_zulip_user_id": 0,
        "skipped_disabled": 0,
        "proposals_notified": 0,
    }


def _resolve_users_by_login(login_norms: Iterable[str]) -> dict[str, User]:
    """Map normalized github_login -> User for reachability, matched case-insensitively.

    Proposals key on ``reviewer_login`` exactly as the console does (``__iexact``), so we resolve
    the same way rather than assuming the stored casing matches ``User.github_login``.
    """
    logins = [login for login in login_norms if login]
    if not logins:
        return {}
    predicate = functools.reduce(operator.or_, (Q(github_login__iexact=login) for login in logins))
    users = User.objects.filter(predicate).only("id", "github_login", "zulip_user_id")
    return {_normalize_login(user.github_login): user for user in users if user.github_login}


def _pr_titles(proposals: list[AssignmentProposal]) -> dict[tuple[int, int], str]:
    """Batch PR titles keyed by ``(repository_id, pr_number)`` for nicer digest lines."""
    numbers_by_repo: dict[int, set[int]] = {}
    for proposal in proposals:
        numbers_by_repo.setdefault(int(proposal.repository_id), set()).add(int(proposal.pr_number))
    titles: dict[tuple[int, int], str] = {}
    for repo_id, numbers in numbers_by_repo.items():
        for number, title in PullRequest.objects.filter(repository_id=repo_id, number__in=numbers).values_list("number", "title"):
            titles[(repo_id, int(number))] = title
    return titles


def _format_expiry(expires_at: datetime, now: datetime) -> str:
    """Absolute (Zulip-localized) deadline plus a coarse relative hint."""
    tag = format_global_time(expires_at)
    remaining = (expires_at - now).total_seconds()
    if remaining <= 0:
        return f"expires {tag} (expiring now)"
    days = int(remaining // 86400)
    if days >= 1:
        return f"expires {tag} (in {days}d)"
    hours = max(1, int(remaining // 3600))
    return f"expires {tag} (in {hours}h)"


def _render_digest(
    *,
    reviewer_login: str,
    proposals: list[AssignmentProposal],
    titles: dict[tuple[int, int], str],
    console_url: str,
    now: datetime,
) -> str:
    count = len(proposals)
    lines: list[str] = [
        "### Review assignment proposals awaiting your response",
        "",
        f"You have {count} proposed review assignment{'' if count == 1 else 's'} waiting for your decision.",
        f"Accept or decline them here: {console_url}",
        "",
    ]
    by_repo: dict[str, list[AssignmentProposal]] = {}
    for proposal in proposals:
        label = f"{proposal.repository.owner}/{proposal.repository.name}"
        by_repo.setdefault(label, []).append(proposal)
    for label in sorted(by_repo):
        lines.append(f"#### {label}")
        for proposal in by_repo[label]:
            title = titles.get((int(proposal.repository_id), int(proposal.pr_number))) or f"PR #{proposal.pr_number}"
            url = f"https://github.com/{label}/pull/{proposal.pr_number}"
            lines.append(f"- [#{proposal.pr_number}]({url}) {title}")
            lines.append(f"  - {_format_expiry(proposal.expires_at, now)}")
        lines.append("")
    lines.append(
        "Declining opts you out of that PR. Letting a proposal expire simply passes it to another "
        "reviewer, and you may be proposed again later."
    )
    return "\n".join(lines).strip()


def deliver_assignment_proposals(
    repositories: Iterable[Repository],
    *,
    now: datetime,
    enabled: bool,
    dry_run: bool,
    client: ZulipClient | None = None,
) -> dict[str, Any]:
    """Send one digest DM per reviewer for their un-notified pending proposals across ``repositories``.

    ``dry_run`` computes the would-send set without sending or stamping. When ``enabled`` but not
    ``dry_run``, ``client`` must be provided (the task constructs it); a ``None`` client counts every
    reviewer as ``failed`` rather than raising.
    """
    repos = list(repositories)
    result: dict[str, Any] = {"stats": _empty_stats(), "per_reviewer": []}
    stats = result["stats"]
    if not repos:
        result["status"] = "ok"
        return result

    proposals = list(
        AssignmentProposal.objects.filter(
            repository__in=repos,
            state=AssignmentProposal.STATE_PROPOSED,
            notified_at__isnull=True,
        )
        .select_related("repository")
        .order_by("reviewer_login", "repository__owner", "repository__name", "expires_at", "pr_number")
    )
    stats["pending_proposals"] = len(proposals)
    if not proposals:
        result["status"] = "ok"
        return result

    by_login: dict[str, list[AssignmentProposal]] = {}
    for proposal in proposals:
        by_login.setdefault(_normalize_login(proposal.reviewer_login), []).append(proposal)
    stats["reviewers"] = len(by_login)

    users_by_login = _resolve_users_by_login(by_login.keys())
    titles = _pr_titles(proposals)
    console_url = build_site_url(reverse("console:home"))

    for login_norm, reviewer_proposals in sorted(by_login.items()):
        original_login = reviewer_proposals[0].reviewer_login
        entry: dict[str, Any] = {"reviewer_login": original_login, "proposals": len(reviewer_proposals)}
        user = users_by_login.get(login_norm)
        if user is None:
            stats["skipped_no_user"] += 1
            entry["status"] = "skipped_no_user"
            result["per_reviewer"].append(entry)
            continue
        if user.zulip_user_id is None:
            stats["skipped_no_zulip_user_id"] += 1
            entry["status"] = "skipped_no_zulip_user_id"
            result["per_reviewer"].append(entry)
            continue

        if dry_run:
            stats["would_send"] += 1
            entry["status"] = "would_send"
            result["per_reviewer"].append(entry)
            continue
        if not enabled:
            stats["skipped_disabled"] += 1
            entry["status"] = "skipped_disabled"
            result["per_reviewer"].append(entry)
            continue
        if client is None:
            stats["failed"] += 1
            entry["status"] = "failed_client_init"
            result["per_reviewer"].append(entry)
            continue

        message = _render_digest(
            reviewer_login=original_login,
            proposals=reviewer_proposals,
            titles=titles,
            console_url=console_url,
            now=now,
        )
        stats["attempted"] += 1
        try:
            for chunk in split_message_chunks(content=message, max_chars=MAX_MESSAGE_CHARS):
                client.send_direct_message(to=[int(user.zulip_user_id)], content=chunk)
        except ZulipApiError as exc:
            stats["failed"] += 1
            entry["status"] = "failed_send"
            entry["error"] = str(exc)[:500]
            result["per_reviewer"].append(entry)
            continue

        # Stamp only rows still proposed+un-notified so a concurrent accept/expire is not clobbered.
        stamped = AssignmentProposal.objects.filter(
            id__in=[int(proposal.id) for proposal in reviewer_proposals],
            state=AssignmentProposal.STATE_PROPOSED,
            notified_at__isnull=True,
        ).update(notified_at=now, updated_at=now)
        stats["sent"] += 1
        stats["proposals_notified"] += int(stamped)
        entry["status"] = "sent"
        entry["notified"] = int(stamped)
        result["per_reviewer"].append(entry)

    result["status"] = "ok"
    return result


__all__ = ["deliver_assignment_proposals"]
