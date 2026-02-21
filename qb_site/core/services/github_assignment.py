from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests


@dataclass(frozen=True)
class AssignmentMutationError(RuntimeError):
    code: str
    message: str


@dataclass(frozen=True)
class GitHubAssignmentClient:
    token: str
    api_base_url: str = "https://api.github.com"
    timeout_seconds: int = 20

    def assign(self, *, owner: str, repo: str, number: int, github_login: str) -> tuple[str, ...]:
        return self.assign_many(owner=owner, repo=repo, number=number, github_logins=(github_login,))

    def unassign(self, *, owner: str, repo: str, number: int, github_login: str) -> tuple[str, ...]:
        return self.unassign_many(owner=owner, repo=repo, number=number, github_logins=(github_login,))

    def assign_many(self, *, owner: str, repo: str, number: int, github_logins: tuple[str, ...]) -> tuple[str, ...]:
        return self._mutate_assignees(method="POST", owner=owner, repo=repo, number=number, github_logins=github_logins)

    def unassign_many(self, *, owner: str, repo: str, number: int, github_logins: tuple[str, ...]) -> tuple[str, ...]:
        return self._mutate_assignees(method="DELETE", owner=owner, repo=repo, number=number, github_logins=github_logins)

    def _mutate_assignees(
        self,
        *,
        method: str,
        owner: str,
        repo: str,
        number: int,
        github_logins: tuple[str, ...],
    ) -> tuple[str, ...]:
        url = f"{self.api_base_url.rstrip('/')}/repos/{owner}/{repo}/issues/{number}/assignees"
        payload = {"assignees": list(github_logins)}
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
        }
        response = requests.request(method, url, json=payload, headers=headers, timeout=self.timeout_seconds)
        if response.status_code < 400:
            return _parse_assignees_from_issue_payload(response=response)

        details: dict[str, Any]
        try:
            details = response.json()
        except Exception:
            details = {"raw": response.text}

        if response.status_code in {401, 403}:
            raise AssignmentMutationError("permission_denied", "GitHub permission denied for assignment mutation.")
        if response.status_code == 404:
            raise AssignmentMutationError("pr_not_found", "GitHub pull request was not found for assignment mutation.")
        if response.status_code == 422:
            rendered_logins = ", ".join(f"`{login}`" for login in github_logins)
            raise AssignmentMutationError(
                "validation_failed",
                f"GitHub rejected assignee(s) {rendered_logins} for this pull request.",
            )
        if response.status_code >= 500:
            raise AssignmentMutationError("github_transient", "GitHub API temporarily failed during assignment mutation.")

        raise AssignmentMutationError("github_error", f"GitHub API error during assignment mutation: {details}")


def _parse_assignees_from_issue_payload(*, response: requests.Response) -> tuple[str, ...]:
    try:
        payload = response.json()
    except Exception:
        return ()

    if not isinstance(payload, dict):
        return ()

    assignee_nodes = payload.get("assignees") or []
    return tuple(
        str(node.get("login", "")).strip()
        for node in assignee_nodes
        if isinstance(node, dict) and str(node.get("login", "")).strip()
    )
