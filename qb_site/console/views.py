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

from django.conf import settings
from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from analyzer.models import AssignmentProposal, QueueSnapshot, ReviewerOptOut
from analyzer.services.assignment_proposal_validity import ProposalValidity, proposal_validity, resolve_on_queue_exit_policy
from analyzer.services.queue_rules import default_rule_set_for_repo
from analyzer.services.reviewer_assignment_apply import assign_reviewer_and_record
from analyzer.services.reviewer_assignment_engine import _normalize_login
from console import session as console_session
from core.models import ReviewerPreference
from core.services.github_identity import resolve_or_create_user_from_identity
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

SNAPSHOT_FRESH_SECONDS = 7200


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

    user = resolve_or_create_user_from_identity(identity)
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

    # Preferred labels per repo, to render "why you" (matched topic labels).
    preferred_by_repo: dict[int, set[str]] = {}
    capacity_by_repo: dict[int, int] = {}
    for pref in ReviewerPreference.objects.filter(user=reviewer).only("repository_id", "preferred_labels", "maximum_capacity"):
        preferred_by_repo[pref.repository_id] = {str(lbl).lower() for lbl in (pref.preferred_labels or [])}
        capacity_by_repo[pref.repository_id] = int(pref.maximum_capacity)

    # Batch PR metadata + labels for the proposed PRs, grouped by repo.
    proposal_rows: list[dict] = []
    for proposal in proposals:
        repo = proposal.repository
        pr = PullRequest.objects.filter(repository=repo, number=proposal.pr_number).only("id", "number", "title", "state").first()
        labels = (
            list(PRLabel.objects.filter(pull_request=pr).select_related("label_def").values_list("label_def__name", flat=True))
            if pr is not None
            else []
        )
        preferred = preferred_by_repo.get(repo.id, set())
        matched = sorted({lbl for lbl in labels if lbl and lbl.lower() in preferred})
        proposal_rows.append(
            {
                "proposal": proposal,
                "owner": repo.owner,
                "repo": repo.name,
                "pr_number": proposal.pr_number,
                "title": (pr.title if pr is not None else None) or f"PR #{proposal.pr_number}",
                "url": f"https://github.com/{repo.owner}/{repo.name}/pull/{proposal.pr_number}",
                "matched_labels": matched,
                "expires_at": proposal.expires_at,
            }
        )

    # Load summary per repo: pending proposals + open PRs assigned + capacity.
    pending_by_repo: dict[int, int] = {}
    for proposal in proposals:
        pending_by_repo[proposal.repository_id] = pending_by_repo.get(proposal.repository_id, 0) + 1
    load_rows: list[dict] = []
    for repo_id, capacity in sorted(capacity_by_repo.items()):
        assigned_open = PullRequest.objects.filter(
            repository_id=repo_id, state="open", assignees__contains=[reviewer.github_login]
        ).count()
        load_rows.append(
            {
                "repo_id": repo_id,
                "assigned_open": assigned_open,
                "pending": pending_by_repo.get(repo_id, 0),
                "capacity": capacity,
            }
        )

    return {"proposal_rows": proposal_rows, "load_rows": load_rows, "logout_url": reverse("console:logout")}


# --- accept / decline --------------------------------------------------------


def _queue_membership(repo, *, now) -> tuple[set[int], set[int], bool]:
    rule_set = default_rule_set_for_repo(repo)
    cache_key = str(rule_set.id) if rule_set else "default"
    snapshot = QueueSnapshot.objects.filter(repository=repo, cache_key=cache_key).order_by("-generated_at").first()
    if snapshot is None or not snapshot.payload:
        return set(), set(), False
    fresh = snapshot.generated_at is not None and (now - snapshot.generated_at).total_seconds() <= SNAPSHOT_FRESH_SECONDS
    payload = snapshot.payload
    queue_prs = {int(n) for n in (payload.get("lists", {}).get("dashboards", {}).get("Queue", []) or [])}
    known_prs = {int(n) for n in (payload.get("prs", {}) or {}).keys()}
    return queue_prs, known_prs, fresh


def _live_validity(proposal: AssignmentProposal, *, now) -> tuple[ProposalValidity, PullRequest | None]:
    repo = proposal.repository
    pr_number = int(proposal.pr_number)
    live_pr = PullRequest.objects.filter(repository=repo, number=pr_number).only("id", "number", "state", "assignees").first()
    pr_state = None if live_pr is None else str(live_pr.state)
    current_assignees = set() if live_pr is None else {_normalize_login(str(a)) for a in (live_pr.assignees or []) if a}
    queue_prs, known_prs, fresh = _queue_membership(repo, now=now)
    on_queue = (pr_number in queue_prs) if (fresh and pr_number in known_prs) else None
    validity = proposal_validity(
        proposal,
        now=now,
        pr_state=pr_state,
        current_assignees=current_assignees,
        on_queue=on_queue,
        on_queue_exit=resolve_on_queue_exit_policy(),
    )
    return validity, live_pr


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


_UNAVAILABLE_REASON = {
    "already_terminal": "This proposal has already been decided.",
    "expired": "This proposal has expired.",
    "pr_assigned": "This PR already has an assignee.",
    "pr_closed": "This PR is closed or merged.",
    "pr_off_queue": "This PR is no longer on the review queue.",
}


@require_POST
def accept(request: HttpRequest, proposal_id: int) -> HttpResponse:
    loaded = _load_actionable_proposal(request, proposal_id)
    if isinstance(loaded, HttpResponse):
        return loaded
    reviewer, proposal = loaded
    now = timezone.now()

    if proposal.state != AssignmentProposal.STATE_PROPOSED:
        return render(request, "console/unavailable.html", {"message": _UNAVAILABLE_REASON["already_terminal"]})

    # An explicit prior decline (opt-out) blocks re-acceptance.
    if ReviewerOptOut.objects.filter(
        repository=proposal.repository, pr_number=proposal.pr_number, reviewer_login__iexact=reviewer.github_login, active=True
    ).exists():
        _retire(proposal, ProposalValidity(is_live=False, reason="already_terminal"), now=now)
        return render(request, "console/unavailable.html", {"message": "You have opted out of this PR."})

    validity, _live_pr = _live_validity(proposal, now=now)
    if not validity.is_live:
        _retire(proposal, validity, now=now)
        return render(
            request,
            "console/unavailable.html",
            {"message": _UNAVAILABLE_REASON.get(validity.reason, "This proposal is no longer available.")},
        )

    if not bool(getattr(settings, "ANALYZER_ASSIGNMENT_PROPOSALS_ASSIGN_ON_ACCEPT_ENABLED", False)):
        # Staged rollout: acceptance is not executing GitHub assignments yet. Leave the proposal
        # pending and tell the reviewer, rather than record an acceptance we cannot fulfil.
        return render(request, "console/unavailable.html", {"message": "Accepting isn't enabled yet — please try again later."})

    from core.services.github_operation_tokens import resolve_github_app_operation_token

    token = resolve_github_app_operation_token(
        operation="assign_pr", owner=proposal.repository.owner, repo=proposal.repository.name
    )
    if not token:
        return render(request, "console/unavailable.html", {"message": "Assignment is temporarily unavailable. Try again later."})

    outcome, _client, _record = assign_reviewer_and_record(
        repository=proposal.repository,
        pr_number=int(proposal.pr_number),
        login=proposal.reviewer_login,
        snapshot=proposal.snapshot,
        run_date=now.date(),
        token=token,
    )
    if outcome == "failed":
        return render(
            request, "console/unavailable.html", {"message": "GitHub rejected the assignment. Try again shortly."}, status=502
        )

    # Assignment landed (applied) or was already recorded — mark accepted.
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
        return render(request, "console/unavailable.html", {"message": _UNAVAILABLE_REASON["already_terminal"]})

    with transaction.atomic():
        AssignmentProposal.objects.filter(id=proposal.id, state=AssignmentProposal.STATE_PROPOSED).update(
            state=AssignmentProposal.STATE_DECLINED,
            decided_at=now,
            decided_via=AssignmentProposal.DECIDED_VIA_CONSOLE,
            updated_at=now,
        )
        # Decline == explicit "not this PR" -> permanent per-PR opt-out (reuses builder enforcement).
        ReviewerOptOut.objects.update_or_create(
            repository=proposal.repository,
            pr_number=int(proposal.pr_number),
            reviewer_login=proposal.reviewer_login,
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
