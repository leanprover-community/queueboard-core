from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

import requests
from django.conf import settings

from core.services.github_operation_tokens import resolve_github_app_operation_token

log = logging.getLogger(__name__)

_GITHUB_API_BASE = "https://api.github.com"
_CLOSE_PR_OPERATION = "close_pr"
_CHECK_COLLABORATOR_PERMISSION_OPERATION = "check_collaborator_permission"


class PermissionOutcome(str, Enum):
    PERMITTED = "permitted"
    NOT_PERMITTED = "not_permitted"
    PR_NOT_OPEN = "pr_not_open"
    TOKEN_UNAVAILABLE = "token_unavailable"
    GITHUB_ERROR = "github_error"


@dataclass(frozen=True)
class PermissionCheckResult:
    outcome: PermissionOutcome
    pr_title: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class LivePRDetails:
    title: str
    is_open: bool
    author_login: str
    body: str | None = None
    opened_at: str | None = None
    updated_at: str | None = None
    labels: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ClosePRError(RuntimeError):
    code: str
    message: str


def check_close_pr_permission(
    *,
    github_login: str,
    owner: str,
    repo: str,
    number: int,
) -> PermissionCheckResult:
    """Check whether github_login may close the given PR.

    Fetches PR details to verify it is open and extract the author login.
    If the invoker is the PR author, permission is granted immediately.
    Otherwise calls the collaborator permission endpoint to check for
    write or admin access.

    Returns a PermissionCheckResult with one of the PermissionOutcome values.
    """
    token = _get_token(owner=owner, repo=repo)
    if not token:
        return PermissionCheckResult(
            outcome=PermissionOutcome.TOKEN_UNAVAILABLE,
            message="GitHub App token for close_pr is not available for this repository.",
        )

    pr = _fetch_pr_details(token=token, owner=owner, repo=repo, number=number)
    if pr is None:
        return PermissionCheckResult(
            outcome=PermissionOutcome.GITHUB_ERROR,
            message=f"Could not fetch PR details for {owner}/{repo}#{number} from GitHub.",
        )

    if not pr.is_open:
        return PermissionCheckResult(
            outcome=PermissionOutcome.PR_NOT_OPEN,
            pr_title=pr.title,
            message=f"Pull request {owner}/{repo}#{number} is not open.",
        )

    if github_login.lower() == pr.author_login.lower():
        return PermissionCheckResult(
            outcome=PermissionOutcome.PERMITTED,
            pr_title=pr.title,
        )

    member_token = _get_member_check_token(owner=owner, repo=repo) or token
    collab_permission = _fetch_collaborator_permission(token=member_token, owner=owner, repo=repo, github_login=github_login)
    if collab_permission in {"write", "admin"}:
        return PermissionCheckResult(
            outcome=PermissionOutcome.PERMITTED,
            pr_title=pr.title,
        )

    return PermissionCheckResult(
        outcome=PermissionOutcome.NOT_PERMITTED,
        pr_title=pr.title,
        message=(
            f"GitHub login `{github_login}` does not have permission to close "
            f"{owner}/{repo}#{number} (not the PR author and not a write/admin collaborator)."
        ),
    )


def post_pr_comment(*, owner: str, repo: str, number: int, body: str) -> None:
    """Post a comment on a pull request before closing it.

    Uses the close_pr operation token (Issues: Read and write already granted).
    Raises ClosePRError on failure.
    """
    token = _get_token(owner=owner, repo=repo)
    if not token:
        raise ClosePRError(
            code="token_unavailable",
            message="GitHub App token for close_pr is not available for this repository.",
        )

    api_base = _api_base_url()
    url = f"{api_base}/repos/{owner}/{repo}/issues/{number}/comments"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        response = requests.post(url, json={"body": body}, headers=headers, timeout=20)
    except requests.RequestException as exc:
        raise ClosePRError(code="request_failed", message=f"GitHub request failed: {exc}") from exc

    if response.status_code < 400:
        return

    details = _safe_json(response)
    if response.status_code in {401, 403}:
        raise ClosePRError(code="permission_denied", message="GitHub permission denied when posting comment.")
    if response.status_code == 404:
        raise ClosePRError(code="pr_not_found", message="Pull request not found on GitHub.")
    if response.status_code >= 500:
        raise ClosePRError(code="github_transient", message="GitHub API temporarily failed.")
    raise ClosePRError(code="github_error", message=f"GitHub API error when posting comment: {details}")


def fetch_pr_details_for_form(*, owner: str, repo: str, number: int) -> LivePRDetails | None:
    """Fetch PR details for display on the confirmation form.

    Uses the close_pr operation token. Returns None if the token is unavailable
    or the GitHub request fails.
    """
    token = _get_token(owner=owner, repo=repo)
    if not token:
        return None
    return _fetch_pr_details(token=token, owner=owner, repo=repo, number=number)


def close_pull_request(*, owner: str, repo: str, number: int) -> None:
    """Close a pull request via the GitHub API.

    Raises ClosePRError on failure.
    """
    token = _get_token(owner=owner, repo=repo)
    if not token:
        raise ClosePRError(
            code="token_unavailable",
            message="GitHub App token for close_pr is not available for this repository.",
        )

    api_base = _api_base_url()
    url = f"{api_base}/repos/{owner}/{repo}/pulls/{number}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        response = requests.patch(url, json={"state": "closed"}, headers=headers, timeout=20)
    except requests.RequestException as exc:
        raise ClosePRError(code="request_failed", message=f"GitHub request failed: {exc}") from exc

    if response.status_code < 400:
        return

    details = _safe_json(response)
    if response.status_code in {401, 403}:
        raise ClosePRError(code="permission_denied", message="GitHub permission denied when closing pull request.")
    if response.status_code == 404:
        raise ClosePRError(code="pr_not_found", message="Pull request not found on GitHub.")
    if response.status_code == 422:
        raise ClosePRError(code="validation_failed", message=f"GitHub rejected the close request: {details}")
    if response.status_code >= 500:
        raise ClosePRError(code="github_transient", message="GitHub API temporarily failed.")
    raise ClosePRError(code="github_error", message=f"GitHub API error when closing pull request: {details}")


def _get_token(*, owner: str, repo: str) -> str | None:
    return resolve_github_app_operation_token(
        operation=_CLOSE_PR_OPERATION,
        owner=owner,
        repo=repo,
    )


def _get_member_check_token(*, owner: str, repo: str) -> str | None:
    return resolve_github_app_operation_token(
        operation=_CHECK_COLLABORATOR_PERMISSION_OPERATION,
        owner=owner,
        repo=repo,
    )


def _fetch_pr_details(*, token: str, owner: str, repo: str, number: int) -> LivePRDetails | None:
    api_base = _api_base_url()
    url = f"{api_base}/repos/{owner}/{repo}/pulls/{number}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        response = requests.get(url, headers=headers, timeout=20)
    except requests.RequestException:
        log.warning("close_pr_fetch_pr_details_failed", extra={"owner": owner, "repo": repo, "number": number})
        return None

    if response.status_code >= 400:
        log.warning(
            "close_pr_fetch_pr_details_http_error",
            extra={"owner": owner, "repo": repo, "number": number, "status": response.status_code},
        )
        return None

    payload = _safe_json(response)
    title = str(payload.get("title") or "").strip() or f"{owner}/{repo}#{number}"
    state = str(payload.get("state") or "").strip().lower()
    merged_at = payload.get("merged_at")
    is_open = state == "open" and not merged_at
    author_login = str((payload.get("user") or {}).get("login") or "").strip()
    body = str(payload.get("body") or "").strip() or None
    opened_at = str(payload.get("created_at") or "").strip() or None
    updated_at = str(payload.get("updated_at") or "").strip() or None
    raw_labels = payload.get("labels") if isinstance(payload.get("labels"), list) else []
    labels: tuple[tuple[str, str], ...] = tuple(
        (str(lbl.get("name", "")), str(lbl.get("color", ""))) for lbl in raw_labels if isinstance(lbl, dict) and lbl.get("name")
    )
    return LivePRDetails(
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
    The GitHub API returns "none" for users with no access (not 404).
    Possible return values: "admin", "write", "read", "none".
    Note: the "maintain" role maps to "write" in this endpoint's permission field.
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
            "close_pr_collaborator_permission_request_failed",
            extra={"owner": owner, "repo": repo, "github_login": github_login},
        )
        return "none"

    if response.status_code == 404:
        # User is not a collaborator on this repo.
        return "none"

    if response.status_code >= 400:
        log.warning(
            "close_pr_collaborator_permission_http_error",
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
