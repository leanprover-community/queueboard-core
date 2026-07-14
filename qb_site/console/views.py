"""Reviewer console views (design doc 050).

GitHub-OAuth authenticated, session-backed console where a ``confirm``-mode reviewer sees the
assignment proposals awaiting their decision and accepts/declines them. Accept re-validates against
live state and reuses the verbatim 046 mutation path (``assign_reviewer_and_record``) to perform the
GitHub assignment; decline records a permanent per-PR opt-out. Every POST re-validates, so a
proposal that is no longer actionable renders a clear "no longer available" instead of erroring.
"""

from __future__ import annotations

import logging
import secrets
from typing import Iterable

from django.conf import settings
from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from analyzer.models import AssignmentProposal, ReviewerAssignmentApplication, ReviewerOptOut
from analyzer.services.assignment_proposal_validity import (
    ProposalValidity,
    live_proposal_validity,
    queue_membership,
    resolve_on_queue_exit_policy,
)
from analyzer.services.reviewer_assignment import _opt_outs_for_prs
from analyzer.services.reviewer_assignment_apply import assign_reviewer_and_record
from analyzer.services.reviewer_assignment_engine import _normalize_login
from analyzer.services.reviewer_attention import build_reviewer_attention_reports
from analyzer.services.reviewer_attention_format import (
    format_compact_duration,
    sort_by_assignment_recency,
    sort_by_queue_age,
)
from analyzer.services.reviewer_load import (
    format_load_contribution,
    format_load_line,
    reviewer_load_with_breakdown,
)
from console import session as console_session
from core.models import Repository, ReviewerPreference
from core.services.github_assignment import AssignmentMutationError, GitHubAssignmentClient
from core.services.github_identity import resolve_user_from_identity
from core.services.github_oauth import GitHubOAuthClient, GitHubOAuthError
from core.services.oauth_state import (
    ConsoleOAuthStateClaims,
    SignedStateError,
    issue_console_oauth_state,
    validate_console_oauth_state,
)
from core.services.site_urls import build_site_url
from syncer.models import PRLabel, PullRequest

log = logging.getLogger(__name__)


# --- auth --------------------------------------------------------------------


def _safe_next(request: HttpRequest, raw: str | None) -> str:
    """Return a safe same-site ``next`` path, defaulting to the console home."""
    default = reverse("console:home")
    if not raw:
        return default
    if url_has_allowed_host_and_scheme(raw, allowed_hosts={request.get_host()}) and raw.startswith("/"):
        return raw
    return default


@require_GET
def login(request: HttpRequest) -> HttpResponse:
    """Start GitHub OAuth: stash a CSRF nonce in the session and redirect to GitHub."""
    next_path = _safe_next(request, request.GET.get("next"))
    try:
        client = GitHubOAuthClient()
    except GitHubOAuthError:
        log.warning("console.login: GitHub OAuth is not configured")
        return render(request, "console/error.html", {"message": "Sign-in is not configured on this server."}, status=503)

    nonce = secrets.token_urlsafe(24)
    console_session.set_oauth_nonce(request, nonce)
    state = issue_console_oauth_state(claims=ConsoleOAuthStateClaims(nonce=nonce, next=next_path))
    redirect_uri = build_site_url(reverse("console:oauth-callback"))
    return redirect(client.build_authorize_url(state=state, redirect_uri=redirect_uri))


@require_GET
def oauth_callback(request: HttpRequest) -> HttpResponse:
    """Complete GitHub OAuth: verify state+nonce, resolve the reviewer, open a session."""
    code = request.GET.get("code")
    state = request.GET.get("state")
    expected_nonce = console_session.pop_oauth_nonce(request)
    if not code or not state:
        return render(request, "console/error.html", {"message": "Sign-in was cancelled or failed."}, status=400)
    try:
        claims = validate_console_oauth_state(state)
    except SignedStateError:
        return render(request, "console/error.html", {"message": "Sign-in link expired or was invalid. Try again."}, status=400)
    # CSRF: the state's nonce must match the one we stored in this browser's session.
    if not expected_nonce or not secrets.compare_digest(expected_nonce, claims.nonce):
        return render(request, "console/error.html", {"message": "Sign-in could not be verified. Try again."}, status=400)

    try:
        client = GitHubOAuthClient()
        redirect_uri = build_site_url(reverse("console:oauth-callback"))
        token = client.exchange_code_for_access_token(code=code, redirect_uri=redirect_uri)
        identity = client.fetch_user_identity(access_token=token)
    except GitHubOAuthError:
        log.warning("console.oauth_callback: GitHub OAuth exchange failed", exc_info=True)
        return render(request, "console/error.html", {"message": "GitHub sign-in failed. Try again."}, status=502)

    # Resolve-only by construction: the console is for people we already know (registered via the
    # Zulip flow, or ingested by the syncer). A GitHub account we have never seen is not given a
    # session, so the public sign-in URL cannot mint a core.User row for an arbitrary stranger.
    user = resolve_user_from_identity(identity)
    if user is None:
        log.info("console.oauth_callback: unknown GitHub login %r denied", identity.github_login)
        return render(
            request,
            "console/error.html",
            {
                "message": (
                    "This console is only for registered reviewers. If you review for a tracked "
                    "repository, register with the Zulip bot first, then sign in here."
                )
            },
            status=403,
        )
    console_session.set_reviewer(request, user)
    return redirect(_safe_next(request, claims.next))


@require_POST
def logout(request: HttpRequest) -> HttpResponse:
    console_session.clear_reviewer(request)
    return redirect(reverse("console:home"))


# --- console -----------------------------------------------------------------


def _login_url(next_path: str) -> str:
    return f"{reverse('console:login')}?next={next_path}"


@require_GET
def home(request: HttpRequest) -> HttpResponse:
    reviewer = console_session.get_reviewer(request)
    if reviewer is None:
        return render(request, "console/login.html", {"login_url": _login_url(reverse("console:home"))})

    context = _build_home_context(reviewer)
    context["reviewer"] = reviewer
    return render(request, "console/home.html", context)


def _build_home_context(reviewer) -> dict:
    proposals = list(
        AssignmentProposal.objects.filter(
            reviewer_login__iexact=reviewer.github_login,
            state=AssignmentProposal.STATE_PROPOSED,
        )
        .select_related("repository")
        .order_by("repository__owner", "repository__name", "expires_at", "pr_number")
    )

    # Repos in scope: everywhere the reviewer has a preference (source of assigned-PR status +
    # capacity), plus any repo that has a proposal for them (defensive — normally a subset).
    preferred_by_repo: dict[int, set[str]] = {}
    repos_by_id: dict[int, object] = {}
    for pref in (
        ReviewerPreference.objects.filter(user=reviewer)
        .select_related("repository")
        .only("repository_id", "preferred_labels", "repository__owner", "repository__name")
    ):
        preferred_by_repo[int(pref.repository_id)] = {str(lbl).lower() for lbl in (pref.preferred_labels or [])}
        repos_by_id[int(pref.repository_id)] = pref.repository
    for proposal in proposals:
        repos_by_id.setdefault(int(proposal.repository_id), proposal.repository)

    # Batch proposal PR metadata + labels once (was a query per proposal).
    prs_by_repo_number = _batch_prs(proposals)
    labels_by_pr_id = _batch_labels(prs_by_repo_number.values())

    # Each pending proposal occupies capacity at the same weight the engine / load service count it
    # (design doc 050), so surface that contribution next to the proposal.
    proposal_weight = float(getattr(settings, "ANALYZER_ASSIGNMENT_PROPOSAL_PENDING_LOAD_WEIGHT", 1.0))

    # Proposals grouped by repo, preserving the (owner, name, expires_at, pr_number) ordering above.
    proposals_by_repo: dict[int, list[dict]] = {}
    for proposal in proposals:
        repo = proposal.repository
        pr = prs_by_repo_number.get((repo.id, int(proposal.pr_number)))
        labels = labels_by_pr_id.get(pr.id, []) if pr is not None else []
        preferred = preferred_by_repo.get(int(repo.id), set())
        matched = sorted({lbl for lbl in labels if lbl and lbl.lower() in preferred})
        proposals_by_repo.setdefault(int(repo.id), []).append(
            {
                "proposal": proposal,
                "pr_number": proposal.pr_number,
                "title": (pr.title if pr is not None else None) or f"PR #{proposal.pr_number}",
                "url": f"https://github.com/{repo.owner}/{repo.name}/pull/{proposal.pr_number}",
                "matched_labels": matched,
                "expires_at": proposal.expires_at,
                # ISO-8601 (UTC) for client-side rendering in the viewer's own timezone; the template
                # keeps a UTC text fallback for no-JS.
                "expires_at_iso": proposal.expires_at.isoformat() if proposal.expires_at else "",
                "load_weight": format_load_contribution(proposal_weight),
            }
        )

    # Assigned open PRs with status, from the shared reviewer-attention reports (design doc 050:
    # the console is a dashboard, not just a proposal inbox).
    assigned_by_repo: dict[int, list[dict]] = {}
    for repo_id, repo in repos_by_id.items():
        reports = build_reviewer_attention_reports(repository=repo)
        report = next((entry for entry in reports if entry.reviewer_user_id == reviewer.id), None)
        if report is None or not report.items:
            continue
        ordered = list(sort_by_queue_age([i for i in report.items if i.is_on_queue])) + list(
            sort_by_assignment_recency([i for i in report.items if not i.is_on_queue])
        )
        assigned_by_repo[repo_id] = [_assigned_pr_row(repo, item) for item in ordered]

    # A repo earns a section only when there is something to show: a proposal or an assigned PR.
    active_repo_ids = set(proposals_by_repo) | set(assigned_by_repo)
    repo_groups: list[dict] = []
    for repo_id in sorted(active_repo_ids, key=lambda rid: (repos_by_id[rid].owner, repos_by_id[rid].name)):
        repo = repos_by_id[repo_id]
        # Load + per-PR breakdown come from one snapshot read, so the per-row contributions sum to the
        # assigned share of the aggregate load line (design doc 050).
        load, breakdown = reviewer_load_with_breakdown(repo, reviewer.github_login)
        assigned_rows = assigned_by_repo.get(repo_id, [])
        for row in assigned_rows:
            weight = breakdown.get(int(row["pr_number"]))
            row["load_weight"] = format_load_contribution(weight) if weight is not None else None
        repo_groups.append(
            {
                "repo_id": repo_id,
                "repo_label": f"{repo.owner}/{repo.name}",
                "proposals": proposals_by_repo.get(repo_id, []),
                "assigned_prs": assigned_rows,
                "load": load,
                "load_line": format_load_line(load) if load is not None else None,
            }
        )

    return {
        "repo_groups": repo_groups,
        "logout_url": reverse("console:logout"),
        "unassign_enabled": bool(getattr(settings, "ANALYZER_ASSIGNMENT_PROPOSALS_CONSOLE_UNASSIGN_ENABLED", False)),
    }


def _assigned_pr_row(repo, item) -> dict:
    """Flatten a ``ReviewerAttentionItem`` into template-ready assigned-PR display fields."""
    status_bits: list[str] = ["On queue" if item.is_on_queue else "Not on queue"]
    if item.is_on_queue and item.days_on_queue_since_assignment is not None:
        consecutive = format_compact_duration(int(item.days_on_queue_since_assignment) * 86400)
        status_bits.append(f"{consecutive} since assignment")
    if item.total_queue_seconds is not None:
        status_bits.append(f"total {format_compact_duration(int(item.total_queue_seconds))}")

    if item.needs_auto_unassign:
        flag = "auto-unassign soon"
    elif item.needs_nudge:
        flag = "stale"
    else:
        flag = ""

    return {
        "pr_number": item.pr_number,
        "title": item.pr_title or f"PR #{item.pr_number}",
        "url": f"https://github.com/{repo.owner}/{repo.name}/pull/{item.pr_number}",
        "is_on_queue": item.is_on_queue,
        "status": " · ".join(status_bits),
        "flag": flag,
    }


def _batch_prs(proposals: list[AssignmentProposal]) -> dict[tuple[int, int], PullRequest]:
    """Fetch the proposed PRs keyed by ``(repository_id, number)`` in one query per repo."""
    numbers_by_repo: dict[int, set[int]] = {}
    for proposal in proposals:
        numbers_by_repo.setdefault(int(proposal.repository_id), set()).add(int(proposal.pr_number))
    out: dict[tuple[int, int], PullRequest] = {}
    for repo_id, numbers in numbers_by_repo.items():
        for pr in PullRequest.objects.filter(repository_id=repo_id, number__in=numbers).only("id", "number", "title", "state"):
            out[(repo_id, int(pr.number))] = pr
    return out


def _batch_labels(prs: Iterable[PullRequest]) -> dict[int, list[str]]:
    """Map ``pull_request_id -> [label name, ...]`` for the given PRs in a single query."""
    pr_ids = [pr.id for pr in prs]
    if not pr_ids:
        return {}
    out: dict[int, list[str]] = {}
    rows = (
        PRLabel.objects.filter(pull_request_id__in=pr_ids)
        .select_related("label_def")
        .values_list("pull_request_id", "label_def__name")
    )
    for pr_id, name in rows:
        if name:
            out.setdefault(int(pr_id), []).append(name)
    return out


# --- accept / decline --------------------------------------------------------


def _pr_url(repository, number: int) -> str:
    """GitHub PR page URL for ``repository`` #``number``."""
    return f"https://github.com/{repository.owner}/{repository.name}/pull/{int(number)}"


def _live_pr_for(proposal: AssignmentProposal) -> PullRequest | None:
    """Fetch the proposal's PR with just the fields the console needs to reason about it."""
    return (
        PullRequest.objects.filter(repository=proposal.repository, number=int(proposal.pr_number))
        .only("id", "number", "state", "assignees")
        .first()
    )


def _live_validity(proposal: AssignmentProposal, *, now) -> tuple[ProposalValidity, PullRequest | None]:
    """Run the shared validity authority against live facts for one proposal."""
    repo = proposal.repository
    pr_number = int(proposal.pr_number)
    live_pr = _live_pr_for(proposal)
    validity = live_proposal_validity(
        proposal,
        now=now,
        live_pr=live_pr,
        membership=queue_membership(repo, now=now),
        opt_outs=_opt_outs_for_prs(repo, [pr_number]),
        on_queue_exit=resolve_on_queue_exit_policy(),
    )
    return validity, live_pr


def _can_self_assign(live_pr: PullRequest | None, reviewer) -> bool:
    """Whether the reviewer may still assign *themselves* to this PR.

    True exactly when the PR is open and the reviewer is not already an assignee. This is the single
    precondition behind "assign myself anyway": it naturally covers every recoverable proposal state
    (expired, off-queue, assigned-to-someone-else, opted-out) and excludes the cases where the action
    is meaningless — closed/merged PRs and PRs the reviewer already holds (an accepted proposal).
    """
    if live_pr is None:
        return False
    if str(live_pr.state).strip().lower() != "open":
        return False
    assignees = {_normalize_login(str(login)) for login in (live_pr.assignees or []) if login}
    return _normalize_login(reviewer.github_login) not in assignees


def _render_proposal_unavailable(
    request: HttpRequest,
    *,
    proposal: AssignmentProposal,
    live_pr: PullRequest | None,
    reviewer,
    reason: str | None,
    heading: str | None = None,
    status: int = 200,
) -> HttpResponse:
    """Render ``unavailable.html`` for a proposal-reason page.

    The message names the PR inline as a link (no separate "PR: …" line), and "assign myself anyway"
    is offered when the PR is still self-assignable.
    """
    repo = proposal.repository
    pr_link = format_html(
        '<a href="{}">{}</a>', _pr_url(repo, proposal.pr_number), f"{repo.owner}/{repo.name} #{proposal.pr_number}"
    )
    template = _UNAVAILABLE_REASON_TEMPLATE.get(reason, _UNAVAILABLE_DEFAULT_TEMPLATE)

    can_assign_anyway = _can_self_assign(live_pr, reviewer)
    note = ""
    if can_assign_anyway:
        others_assigned = bool([login for login in (live_pr.assignees or []) if login])
        if reason == "opted_out":
            note = "This will also clear your opt-out for this PR."
        elif others_assigned:
            note = "You’ll be added as an additional assignee."
    return render(
        request,
        "console/unavailable.html",
        {
            "message": format_html(template, pr=pr_link),
            "heading": heading,
            "can_assign_anyway": can_assign_anyway,
            "assign_anyway_url": (reverse("console:assign-anyway", args=[proposal.id]) if can_assign_anyway else ""),
            "assign_anyway_note": note,
        },
        status=status,
    )


def _enqueue_pr_sync(repository: Repository, number: int) -> None:
    """Enqueue a per-PR sync so a console-driven assignee change converges into our state."""
    try:
        from syncer.tasks.sync_tasks import sync_pr_task

        sync_pr_task.delay(int(repository.id), int(number))
    except Exception:  # pragma: no cover - defensive enqueue guard
        log.warning("console: post-mutation sync enqueue failed", extra={"repo_id": repository.id, "number": number})


def _retire(proposal: AssignmentProposal, validity: ProposalValidity, *, now) -> None:
    """Persist a non-live verdict so the stale proposal is cleaned up (idempotent)."""
    if validity.terminal_state is None:
        return
    AssignmentProposal.objects.filter(id=proposal.id, state=AssignmentProposal.STATE_PROPOSED).update(
        state=validity.terminal_state,
        decided_at=now,
        decided_via=validity.decided_via or "",
        updated_at=now,
    )


def _load_actionable_proposal(request: HttpRequest, proposal_id: int):
    """Return (reviewer, proposal) or an ``HttpResponse`` to short-circuit with."""
    reviewer = console_session.get_reviewer(request)
    if reviewer is None:
        return redirect(_login_url(reverse("console:home")))
    proposal = AssignmentProposal.objects.select_related("repository", "snapshot").filter(id=int(proposal_id)).first()
    if proposal is None:
        return render(request, "console/unavailable.html", {"message": "That proposal no longer exists."}, status=404)
    if _normalize_login(proposal.reviewer_login) != _normalize_login(reviewer.github_login):
        return render(
            request, "console/unavailable.html", {"message": "That proposal was made to a different reviewer."}, status=403
        )
    return reviewer, proposal


# Reason -> message, with a ``{pr}`` slot the PR link is woven into so the sentence names the PR
# inline (like the unassigned page) rather than saying a bare "This PR" beside a separate link.
_UNAVAILABLE_REASON_TEMPLATE = {
    "already_terminal": "The proposal for {pr} has already been decided.",
    "expired": "The proposal for {pr} has expired.",
    "pr_assigned": "{pr} already has an assignee.",
    "pr_closed": "{pr} is closed or merged.",
    "pr_off_queue": "{pr} is no longer on the review queue.",
    "opted_out": "You’ve opted out of {pr}, so this proposal no longer applies.",
}
_UNAVAILABLE_DEFAULT_TEMPLATE = "The proposal for {pr} is no longer available."


def _github_assign_self(request: HttpRequest, proposal: AssignmentProposal, *, now) -> tuple[bool, HttpResponse | None]:
    """Perform the shared 046 GitHub assign for ``proposal`` and confirm it landed.

    Returns ``(True, None)`` when the reviewer is now assigned on GitHub, else ``(False, response)``
    with the appropriate ``unavailable`` page. Callers must have already gated on
    ``ANALYZER_ASSIGNMENT_PROPOSALS_ASSIGN_ON_ACCEPT_ENABLED``. Shared by ``accept`` and
    ``assign_anyway`` so the (subtle) "did it actually land?" semantics never drift between them.
    """
    from core.services.github_operation_tokens import resolve_github_app_operation_token

    token = resolve_github_app_operation_token(
        operation="assign_pr", owner=proposal.repository.owner, repo=proposal.repository.name
    )
    if not token:
        return False, render(
            request, "console/unavailable.html", {"message": "Assignment is temporarily unavailable. Try again later."}
        )

    outcome, _client, record = assign_reviewer_and_record(
        repository=proposal.repository,
        pr_number=int(proposal.pr_number),
        login=proposal.reviewer_login,
        snapshot=proposal.snapshot,
        run_date=now.date(),
        token=token,
    )
    # Only treat it as landed when the assignment actually took on GitHub. ``already_recorded`` means a
    # row for (today, repo, pr, reviewer) already existed — a success ONLY when that row is APPLIED.
    # A prior FAILED/PENDING attempt (an earlier click GitHub rejected) must never be reported to the
    # reviewer as an assignment that took. See design doc 050 review.
    landed = outcome == "applied" or (
        outcome == "already_recorded" and record is not None and record.status == ReviewerAssignmentApplication.STATUS_APPLIED
    )
    if not landed:
        return False, render(
            request,
            "console/unavailable.html",
            {
                "heading": "Couldn’t assign you just now",
                "message": "GitHub didn’t confirm the assignment. Please try again in a little while.",
            },
            status=502,
        )
    return True, None


@require_POST
def accept(request: HttpRequest, proposal_id: int) -> HttpResponse:
    loaded = _load_actionable_proposal(request, proposal_id)
    if isinstance(loaded, HttpResponse):
        return loaded
    reviewer, proposal = loaded
    now = timezone.now()

    if proposal.state != AssignmentProposal.STATE_PROPOSED:
        return _render_proposal_unavailable(
            request,
            proposal=proposal,
            live_pr=_live_pr_for(proposal),
            reviewer=reviewer,
            reason="already_terminal",
        )

    validity, live_pr = _live_validity(proposal, now=now)
    if not validity.is_live:
        _retire(proposal, validity, now=now)
        return _render_proposal_unavailable(
            request,
            proposal=proposal,
            live_pr=live_pr,
            reviewer=reviewer,
            reason=validity.reason,
        )

    if not bool(getattr(settings, "ANALYZER_ASSIGNMENT_PROPOSALS_ASSIGN_ON_ACCEPT_ENABLED", False)):
        # Staged rollout: acceptance is not executing GitHub assignments yet. Leave the proposal
        # pending and tell the reviewer, rather than record an acceptance we cannot fulfil.
        return render(request, "console/unavailable.html", {"message": "Accepting isn't enabled yet — please try again later."})

    landed, failure = _github_assign_self(request, proposal, now=now)
    if not landed:
        return failure  # leave the proposal pending so a later daily run (fresh run_date) can retry

    # Assignment landed — mark accepted.
    AssignmentProposal.objects.filter(id=proposal.id, state=AssignmentProposal.STATE_PROPOSED).update(
        state=AssignmentProposal.STATE_ACCEPTED,
        decided_at=now,
        decided_via=AssignmentProposal.DECIDED_VIA_CONSOLE,
        updated_at=now,
    )
    return render(
        request,
        "console/decided.html",
        {
            "action": "accepted",
            "owner": proposal.repository.owner,
            "repo": proposal.repository.name,
            "pr_number": proposal.pr_number,
        },
    )


@require_POST
def assign_anyway(request: HttpRequest, proposal_id: int) -> HttpResponse:
    """Self-assign to a proposal's PR even though the proposal itself is no longer acceptable.

    Reached from the "no longer available" page. Deliberately bypasses the proposal validity gate —
    that is the whole point — but keeps the one honest precondition (``_can_self_assign``: the PR is
    open and the reviewer isn't already on it) and the same per-reviewer ownership check, GitHub
    mutation, and audit trail as ``accept``. On success it also clears any active per-PR opt-out so
    the next builder/expiry pass doesn't undo the assignment the reviewer just asked for.
    """
    loaded = _load_actionable_proposal(request, proposal_id)
    if isinstance(loaded, HttpResponse):
        return loaded
    reviewer, proposal = loaded
    now = timezone.now()

    live_pr = _live_pr_for(proposal)
    if not _can_self_assign(live_pr, reviewer):
        return render(
            request,
            "console/unavailable.html",
            {"message": "This PR is closed or merged, or you’re already assigned to it."},
        )

    if not bool(getattr(settings, "ANALYZER_ASSIGNMENT_PROPOSALS_ASSIGN_ON_ACCEPT_ENABLED", False)):
        return render(request, "console/unavailable.html", {"message": "Assigning isn’t enabled yet — please try again later."})

    landed, failure = _github_assign_self(request, proposal, now=now)
    if not landed:
        return failure

    with transaction.atomic():
        # The reviewer explicitly wants this PR now, so retract any active per-PR opt-out (lowercased
        # like every other ReviewerOptOut writer) — otherwise the builder would keep excluding them.
        ReviewerOptOut.objects.filter(
            repository=proposal.repository,
            pr_number=int(proposal.pr_number),
            reviewer_login=_normalize_login(proposal.reviewer_login),
            active=True,
        ).update(active=False, cleared_at=now)
        # If the proposal is somehow still pending, retire it as accepted for consistency; a no-op
        # (0 rows) in the normal case where it is already terminal.
        AssignmentProposal.objects.filter(id=proposal.id, state=AssignmentProposal.STATE_PROPOSED).update(
            state=AssignmentProposal.STATE_ACCEPTED,
            decided_at=now,
            decided_via=AssignmentProposal.DECIDED_VIA_CONSOLE,
            updated_at=now,
        )
    return render(
        request,
        "console/decided.html",
        {
            "action": "accepted",
            "owner": proposal.repository.owner,
            "repo": proposal.repository.name,
            "pr_number": proposal.pr_number,
        },
    )


@require_POST
def decline(request: HttpRequest, proposal_id: int) -> HttpResponse:
    loaded = _load_actionable_proposal(request, proposal_id)
    if isinstance(loaded, HttpResponse):
        return loaded
    reviewer, proposal = loaded
    now = timezone.now()

    if proposal.state != AssignmentProposal.STATE_PROPOSED:
        return _render_proposal_unavailable(
            request,
            proposal=proposal,
            live_pr=_live_pr_for(proposal),
            reviewer=reviewer,
            reason="already_terminal",
        )

    with transaction.atomic():
        AssignmentProposal.objects.filter(id=proposal.id, state=AssignmentProposal.STATE_PROPOSED).update(
            state=AssignmentProposal.STATE_DECLINED,
            decided_at=now,
            decided_via=AssignmentProposal.DECIDED_VIA_CONSOLE,
            updated_at=now,
        )
        # Decline == explicit "not this PR" -> permanent per-PR opt-out (reuses builder enforcement).
        # Lowercase the login like every other ReviewerOptOut writer/clearer (syncer, backfill): the
        # unique constraint is case-sensitive, so a mixed-case row would duplicate the lowercase one
        # and the syncer's exact-match assign-clearing would never deactivate it.
        ReviewerOptOut.objects.update_or_create(
            repository=proposal.repository,
            pr_number=int(proposal.pr_number),
            reviewer_login=_normalize_login(proposal.reviewer_login),
            defaults={"active": True, "opted_out_at": now, "cleared_at": None},
        )
    return render(
        request,
        "console/decided.html",
        {
            "action": "declined",
            "owner": proposal.repository.owner,
            "repo": proposal.repository.name,
            "pr_number": proposal.pr_number,
        },
    )


# --- unassign ----------------------------------------------------------------


@require_POST
def unassign(request: HttpRequest) -> HttpResponse:
    """Remove the signed-in reviewer from one or more PRs they are assigned to (self-service only).

    Posts a ``repo_id`` and one or more ``pr_numbers`` from the home dashboard's assigned-PR form.
    Gated by ``ANALYZER_ASSIGNMENT_PROPOSALS_CONSOLE_UNASSIGN_ENABLED``. The login removed is always
    the *authenticated reviewer's own* — never taken from the request — so this surface can only ever
    unassign the person operating it. Each success enqueues a per-PR sync so state converges.
    """
    reviewer = console_session.get_reviewer(request)
    if reviewer is None:
        return redirect(_login_url(reverse("console:home")))

    if not bool(getattr(settings, "ANALYZER_ASSIGNMENT_PROPOSALS_CONSOLE_UNASSIGN_ENABLED", False)):
        return render(request, "console/unavailable.html", {"message": "Unassigning isn’t enabled yet — please try again later."})

    repo = Repository.objects.filter(id=request.POST.get("repo_id") or 0).only("id", "owner", "name").first()
    if repo is None:
        return render(request, "console/unavailable.html", {"message": "That repository was not found."}, status=404)

    numbers: set[int] = set()
    for raw in request.POST.getlist("pr_numbers"):
        try:
            numbers.add(int(raw))
        except (TypeError, ValueError):
            continue
    if not numbers:
        # Nothing selected — just return to the dashboard rather than render an error.
        return redirect(reverse("console:home"))

    from core.services.github_operation_tokens import resolve_github_app_operation_token

    token = resolve_github_app_operation_token(operation="unassign_pr", owner=repo.owner, repo=repo.name)
    if not token:
        return render(
            request, "console/unavailable.html", {"message": "Unassignment is temporarily unavailable. Try again later."}
        )

    client = GitHubAssignmentClient(token=token)
    login = reviewer.github_login
    login_norm = _normalize_login(login)
    unassigned: list[int] = []
    failed: list[int] = []
    for number in sorted(numbers):
        try:
            resulting = client.unassign(owner=repo.owner, repo=repo.name, number=number, github_login=login)
        except AssignmentMutationError:
            failed.append(number)
            continue
        # Confirm we actually left the assignee set (DELETE is idempotent, so a login that was never
        # there also succeeds — that's fine, the reviewer is not assigned either way).
        if login_norm in {_normalize_login(str(assignee)) for assignee in resulting if assignee}:
            failed.append(number)
            continue
        unassigned.append(number)
        _enqueue_pr_sync(repo, number)

    return render(
        request,
        "console/unassigned.html",
        {
            "repo_label": f"{repo.owner}/{repo.name}",
            "unassigned": [{"number": n, "url": _pr_url(repo, n)} for n in unassigned],
            "failed": [{"number": n, "url": _pr_url(repo, n)} for n in failed],
        },
    )
