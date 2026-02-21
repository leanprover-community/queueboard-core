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

    def assign(self, *, owner: str, repo: str, number: int, github_login: str) -> None:
        self._mutate_assignee(method="POST", owner=owner, repo=repo, number=number, github_login=github_login)

    def unassign(self, *, owner: str, repo: str, number: int, github_login: str) -> None:
        self._mutate_assignee(method="DELETE", owner=owner, repo=repo, number=number, github_login=github_login)

    def _mutate_assignee(self, *, method: str, owner: str, repo: str, number: int, github_login: str) -> None:
        url = f"{self.api_base_url.rstrip('/')}/repos/{owner}/{repo}/issues/{number}/assignees"
        payload = {"assignees": [github_login]}
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
        }
        response = requests.request(method, url, json=payload, headers=headers, timeout=self.timeout_seconds)
        if response.status_code < 400:
            return

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
            raise AssignmentMutationError(
                "validation_failed",
                f"GitHub rejected assignee `{github_login}` for this pull request.",
            )
        if response.status_code >= 500:
            raise AssignmentMutationError("github_transient", "GitHub API temporarily failed during assignment mutation.")

        raise AssignmentMutationError("github_error", f"GitHub API error during assignment mutation: {details}")
