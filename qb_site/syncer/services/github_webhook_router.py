from __future__ import annotations

from typing import Any


def _repo_identity(payload: dict[str, Any]) -> tuple[str, str]:
    repo = payload.get("repository") if isinstance(payload.get("repository"), dict) else {}
    owner = (repo.get("owner") or {}) if isinstance(repo, dict) else {}
    owner_login = str(owner.get("login") or "") if isinstance(owner, dict) else ""
    repo_name = str(repo.get("name") or "") if isinstance(repo, dict) else ""
    return owner_login, repo_name


def route_github_webhook(*, event: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Classify GitHub webhook payload into a structured routing summary.

    This chunk only parses and classifies events; task fanout is added later.
    """
    action = str(payload.get("action") or "")
    repo_owner, repo_name = _repo_identity(payload)
    result: dict[str, Any] = {
        "event": event,
        "action": action,
        "repository": {"owner": repo_owner, "name": repo_name},
        "supported_event": False,
        "route": "noop",
        "reason": "unsupported_event",
        "pr_numbers": [],
        "head_sha": "",
    }

    if event == "pull_request":
        pr = payload.get("pull_request") if isinstance(payload.get("pull_request"), dict) else {}
        number = pr.get("number") if isinstance(pr, dict) else None
        pr_numbers = [int(number)] if isinstance(number, int) else []
        result.update(
            {
                "supported_event": True,
                "route": "pull_request",
                "reason": "parsed",
                "pr_numbers": pr_numbers,
            }
        )
        return result

    if event in {"check_run", "check_suite"}:
        key = "check_run" if event == "check_run" else "check_suite"
        check = payload.get(key) if isinstance(payload.get(key), dict) else {}
        head_sha = str(check.get("head_sha") or "") if isinstance(check, dict) else ""

        pr_numbers: list[int] = []
        raw_prs = check.get("pull_requests") if isinstance(check, dict) else None
        if isinstance(raw_prs, list):
            for item in raw_prs:
                if not isinstance(item, dict):
                    continue
                number = item.get("number")
                if isinstance(number, int):
                    pr_numbers.append(number)

        result.update(
            {
                "supported_event": True,
                "route": "check",
                "reason": "parsed",
                "pr_numbers": sorted(set(pr_numbers)),
                "head_sha": head_sha,
            }
        )
        return result

    return result
