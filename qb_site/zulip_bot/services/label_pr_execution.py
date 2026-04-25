from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import requests
from django.conf import settings

from core.services.github_operation_tokens import resolve_github_app_operation_token
from zulip_bot.services.close_pr_execution import PermissionCheckResult, PermissionOutcome

log = logging.getLogger(__name__)

_GITHUB_API_BASE = "https://api.github.com"
_LABEL_PR_OPERATION = "label_pr"
_CHECK_COLLABORATOR_PERMISSION_OPERATION = "check_collaborator_permission"


@dataclass(frozen=True)
class LiveIssueDetails:
    title: str
    is_open: bool
    author_login: str
    body: str | None = None
    opened_at: str | None = None
    updated_at: str | None = None
    labels: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class LabelPRError(RuntimeError):
    code: str
    message: str


def check_label_pr_permission(
    *,
    github_login: str,
    owner: str,
    repo: str,
    number: int,
) -> PermissionCheckResult:
    """Check whether github_login may label the given issue or PR.

    Requires write or admin collaborator access; authorship alone is not sufficient.
    Uses the issues endpoint so the check works for both PRs and plain issues.
    """
    token = _get_token(owner=owner, repo=repo)
    if not token:
        return PermissionCheckResult(
            outcome=PermissionOutcome.TOKEN_UNAVAILABLE,
            message="GitHub App token for label_pr is not available for this repository.",
        )

    issue = _fetch_issue_details(token=token, owner=owner, repo=repo, number=number)
    if issue is None:
        return PermissionCheckResult(
            outcome=PermissionOutcome.GITHUB_ERROR,
            message=f"Could not fetch issue/PR details for {owner}/{repo}#{number} from GitHub.",
        )

    if not issue.is_open:
        return PermissionCheckResult(
            outcome=PermissionOutcome.PR_NOT_OPEN,
            pr_title=issue.title,
            message=f"Issue/PR {owner}/{repo}#{number} is not open.",
        )

    member_token = _get_member_check_token(owner=owner, repo=repo) or token
    collab_permission = _fetch_collaborator_permission(token=member_token, owner=owner, repo=repo, github_login=github_login)
    if collab_permission in {"write", "admin"}:
        return PermissionCheckResult(
            outcome=PermissionOutcome.PERMITTED,
            pr_title=issue.title,
        )

    return PermissionCheckResult(
        outcome=PermissionOutcome.NOT_PERMITTED,
        pr_title=issue.title,
        message=(
            f"GitHub login `{github_login}` does not have permission to label "
            f"{owner}/{repo}#{number} (write or admin access required)."
        ),
    )


def fetch_issue_details_for_form(*, owner: str, repo: str, number: int) -> LiveIssueDetails | None:
    """Fetch issue/PR details for display on the label form."""
    token = _get_token(owner=owner, repo=repo)
    if not token:
        return None
    return _fetch_issue_details(token=token, owner=owner, repo=repo, number=number)


def fetch_repo_labels_from_db(*, owner: str, repo: str) -> list:
    """Return all LabelDef entries for the repo, sorted by name."""
    from syncer.models import LabelDef

    return list(
        LabelDef.objects.filter(
            repository__owner=owner,
            repository__name=repo,
        ).order_by("name")
    )


def set_pr_labels(*, owner: str, repo: str, number: int, label_names: list[str]) -> None:
    """Replace all labels on an issue/PR with the given set.

    Uses PUT which replaces the full label set. Raises LabelPRError on failure.
    """
    token = _get_token(owner=owner, repo=repo)
    if not token:
        raise LabelPRError(
            code="token_unavailable",
            message="GitHub App token for label_pr is not available for this repository.",
        )

    api_base = _api_base_url()
    url = f"{api_base}/repos/{owner}/{repo}/issues/{number}/labels"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        response = requests.put(url, json={"labels": label_names}, headers=headers, timeout=20)
    except requests.RequestException as exc:
        raise LabelPRError(code="request_failed", message=f"GitHub request failed: {exc}") from exc

    if response.status_code < 400:
        return

    details = _safe_json(response)
    if response.status_code in {401, 403}:
        raise LabelPRError(code="permission_denied", message="GitHub permission denied when setting labels.")
    if response.status_code == 404:
        raise LabelPRError(code="not_found", message="Issue/PR not found on GitHub.")
    if response.status_code == 422:
        raise LabelPRError(code="validation_failed", message=f"GitHub rejected the label request: {details}")
    if response.status_code >= 500:
        raise LabelPRError(code="github_transient", message="GitHub API temporarily failed.")
    raise LabelPRError(code="github_error", message=f"GitHub API error when setting labels: {details}")


def _get_token(*, owner: str, repo: str) -> str | None:
    return resolve_github_app_operation_token(
        operation=_LABEL_PR_OPERATION,
        owner=owner,
        repo=repo,
    )


def _get_member_check_token(*, owner: str, repo: str) -> str | None:
    return resolve_github_app_operation_token(
        operation=_CHECK_COLLABORATOR_PERMISSION_OPERATION,
        owner=owner,
        repo=repo,
    )


def _fetch_issue_details(*, token: str, owner: str, repo: str, number: int) -> LiveIssueDetails | None:
    api_base = _api_base_url()
    url = f"{api_base}/repos/{owner}/{repo}/issues/{number}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        response = requests.get(url, headers=headers, timeout=20)
    except requests.RequestException:
        log.warning("label_pr_fetch_issue_details_failed", extra={"owner": owner, "repo": repo, "number": number})
        return None

    if response.status_code >= 400:
        log.warning(
            "label_pr_fetch_issue_details_http_error",
            extra={"owner": owner, "repo": repo, "number": number, "status": response.status_code},
        )
        return None

    payload = _safe_json(response)
    title = str(payload.get("title") or "").strip() or f"{owner}/{repo}#{number}"
    state = str(payload.get("state") or "").strip().lower()
    is_open = state == "open"
    author_login = str((payload.get("user") or {}).get("login") or "").strip()
    body = str(payload.get("body") or "").strip() or None
    opened_at = str(payload.get("created_at") or "").strip() or None
    updated_at = str(payload.get("updated_at") or "").strip() or None
    raw_labels = payload.get("labels") if isinstance(payload.get("labels"), list) else []
    labels: tuple[tuple[str, str], ...] = tuple(
        (str(lbl.get("name", "")), str(lbl.get("color", ""))) for lbl in raw_labels if isinstance(lbl, dict) and lbl.get("name")
    )
    return LiveIssueDetails(
        title=title,
        is_open=is_open,
        author_login=author_login,
        body=body,
        opened_at=opened_at,
        updated_at=updated_at,
        labels=labels,
    )


def _fetch_collaborator_permission(*, token: str, owner: str, repo: str, github_login: str) -> str:
    """Return the permission level string for github_login on owner/repo.

    Returns "none" on any error or if the user is not a collaborator.
    Possible values: "admin", "write", "read", "none".
    Note: triage role returns "read" from this endpoint (known limitation).
    """
    api_base = _api_base_url()
    url = f"{api_base}/repos/{owner}/{repo}/collaborators/{github_login}/permission"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        response = requests.get(url, headers=headers, timeout=20)
    except requests.RequestException:
        log.warning(
            "label_pr_collaborator_permission_request_failed",
            extra={"owner": owner, "repo": repo, "github_login": github_login},
        )
        return "none"

    if response.status_code == 404:
        return "none"

    if response.status_code >= 400:
        log.warning(
            "label_pr_collaborator_permission_http_error",
            extra={"owner": owner, "repo": repo, "github_login": github_login, "status": response.status_code},
        )
        return "none"

    payload = _safe_json(response)
    permission = str(payload.get("permission") or "").strip().lower()
    return permission or "none"


def _api_base_url() -> str:
    configured = getattr(settings, "GITHUB_API_URL", "").strip().rstrip("/")
    return configured or _GITHUB_API_BASE


def _safe_json(response: requests.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {"raw": response.text}
    if isinstance(payload, dict):
        return payload
    return {"raw": payload}
