from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import requests
from django.conf import settings

from syncer.models import PullRequest, PullRequestState
from zulip_bot.commands import CommandContext, CommandResult, ResponseMode
from zulip_bot.services.assignment_command_parser import AssignmentCommandParseError, parse_assignment_command_args
from zulip_bot.services.assignment_validation import AssignmentTargetValidation, validate_assignment_targets
from zulip_bot.services.zulip_client import ZulipApiError, ZulipClient


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


def run_assignment_command(*, action: str, context: CommandContext, args: str) -> CommandResult:
    try:
        parsed = parse_assignment_command_args(
            args=args,
            rendered_content=context.rendered_content,
            sender_id=context.sender_id,
        )
    except AssignmentCommandParseError as exc:
        return CommandResult(
            content=f"Could not parse `{action}` command: {exc.message}",
            response_mode=ResponseMode.PRIVATE,
        )

    successes: list[str] = []
    warnings: list[str] = []
    failures: list[str] = []

    if parsed.unresolved_mentions:
        unresolved = ", ".join(parsed.unresolved_mentions)
        warnings.append(f"Unresolved mentions: {unresolved}.")

    validation = validate_assignment_targets(pr=parsed.pr, target_user_ids=parsed.target_user_ids)
    valid_targets = [target for target in validation.targets if target.ok]
    for target in validation.targets:
        if not target.ok:
            failures.append(f"Zulip user {target.zulip_user_id}: {target.message} ({target.code})")

    local_pr = _load_local_pr(owner=parsed.pr.owner, repo=parsed.pr.repo, number=parsed.pr.number)
    if local_pr is not None:
        if local_pr.state != PullRequestState.OPEN:
            failures.append("Pull request is not open in local sync data (state != open).")
            valid_targets = []
        else:
            valid_targets, idempotent_warnings = _apply_local_idempotency(
                action=action,
                local_pr=local_pr,
                valid_targets=valid_targets,
            )
            warnings.extend(idempotent_warnings)

    if not valid_targets:
        failures.append(f"No valid reviewers to {action} after validation.")
        return _summary_response(action=action, successes=successes, warnings=warnings, failures=failures)

    if not _assignment_mutations_enabled():
        github_logins = ", ".join(sorted({target.github_login for target in valid_targets if target.github_login}))
        successes.append(f"Validated targets: `{github_logins}`.")
        successes.append(f"Preflight passed for `{action}` on {parsed.pr.owner}/{parsed.pr.repo}#{parsed.pr.number}.")
        warnings.append("GitHub assignment mutation is disabled (enable ZULIP_ASSIGNMENT_MUTATIONS_ENABLED to execute).")
        return _summary_response(action=action, successes=successes, warnings=warnings, failures=failures)

    token = _assignment_token()
    if not token:
        failures.append("GitHub assignment token is not configured.")
        return _summary_response(action=action, successes=successes, warnings=warnings, failures=failures)

    gh_client = GitHubAssignmentClient(token=token)
    mutation_successes = 0
    for target in valid_targets:
        github_login = target.github_login
        if not github_login:
            failures.append(f"Zulip user {target.zulip_user_id}: missing GitHub login after validation.")
            continue
        try:
            if action == "assign":
                gh_client.assign(owner=parsed.pr.owner, repo=parsed.pr.repo, number=parsed.pr.number, github_login=github_login)
            elif action == "unassign":
                gh_client.unassign(owner=parsed.pr.owner, repo=parsed.pr.repo, number=parsed.pr.number, github_login=github_login)
            else:
                failures.append(f"Unsupported action `{action}`.")
                continue
            mutation_successes += 1
            successes.append(f"{action} succeeded for `{github_login}`.")
        except AssignmentMutationError as exc:
            failures.append(f"{action} failed for `{github_login}`: {exc.message} ({exc.code})")

    if mutation_successes > 0:
        _try_add_success_reaction(context=context, warnings=warnings)

    if mutation_successes > 0 and not warnings and not failures:
        return CommandResult(content="", response_mode=ResponseMode.PRIVATE, response_not_required=True)

    return _summary_response(action=action, successes=successes, warnings=warnings, failures=failures)


def _summary_response(*, action: str, successes: list[str], warnings: list[str], failures: list[str]) -> CommandResult:
    lines = [f"Summary for `{action}`:"]
    if successes:
        lines.append("Successes:")
        lines.extend(f"- {entry}" for entry in successes)
    if warnings:
        lines.append("Warnings:")
        lines.extend(f"- {entry}" for entry in warnings)
    if failures:
        lines.append("Failures:")
        lines.extend(f"- {entry}" for entry in failures)
    return CommandResult(content="\n".join(lines), response_mode=ResponseMode.PRIVATE)


def _load_local_pr(*, owner: str, repo: str, number: int) -> PullRequest | None:
    return (
        PullRequest.objects.filter(repository__owner=owner, repository__name=repo, number=number)
        .only("id", "state", "assignees")
        .first()
    )


def _apply_local_idempotency(
    *,
    action: str,
    local_pr: PullRequest,
    valid_targets: list[AssignmentTargetValidation],
) -> tuple[list[AssignmentTargetValidation], list[str]]:
    assignees_lc = {str(login).strip().lower() for login in (local_pr.assignees or []) if str(login).strip()}
    remaining: list[AssignmentTargetValidation] = []
    warnings: list[str] = []

    for target in valid_targets:
        login = (target.github_login or "").strip()
        login_lc = login.lower()
        if action == "assign" and login_lc in assignees_lc:
            warnings.append(f"`{login}` is already assigned (local data).")
            continue
        if action == "unassign" and login_lc not in assignees_lc:
            warnings.append(f"`{login}` is not currently assigned (local data).")
            continue
        remaining.append(target)

    return remaining, warnings


def _assignment_mutations_enabled() -> bool:
    value = str(getattr(settings, "ZULIP_ASSIGNMENT_MUTATIONS_ENABLED", "")).strip().lower()
    return value in {"1", "true", "yes", "on"}


def _assignment_token() -> str:
    setting_token = str(getattr(settings, "GITHUB_ASSIGNMENT_TOKEN", "")).strip()
    if setting_token:
        return setting_token

    env_token = os.getenv("GITHUB_ASSIGNMENT_TOKEN", "").strip()
    if env_token:
        return env_token

    env_tokens = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN") or ""
    first = next((token.strip() for token in env_tokens.split(",") if token.strip()), "")
    return first


def _try_add_success_reaction(*, context: CommandContext, warnings: list[str]) -> None:
    if context.message_id is None:
        warnings.append("Mutation succeeded but message_id is missing, so no Zulip reaction was added.")
        return

    emoji_name = str(getattr(settings, "ZULIP_ASSIGNMENT_SUCCESS_EMOJI", "thumbs_up")).strip() or "thumbs_up"
    try:
        ZulipClient().add_reaction(message_id=context.message_id, emoji_name=emoji_name)
    except ZulipApiError:
        warnings.append("Mutation succeeded but adding Zulip reaction failed.")
