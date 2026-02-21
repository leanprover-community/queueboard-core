from __future__ import annotations

import logging
from dataclasses import dataclass
import requests
from django.conf import settings

from core.models import Repository
from core.services.github_assignment import AssignmentMutationError, GitHubAssignmentClient
from core.services.github_operation_tokens import resolve_github_app_operation_token
from syncer.models import PullRequest, PullRequestState
from zulip_bot.commands import CommandContext, CommandResult, ResponseMode
from zulip_bot.services.assignment_command_parser import AssignmentCommandParseError, parse_assignment_command_args
from zulip_bot.services.assignment_validation import AssignmentTargetValidation, validate_assignment_targets

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LivePullRequestView:
    is_open: bool


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
    mention_labels_by_user_id = dict(parsed.mention_labels_by_user_id)
    target_refs_by_zulip_id = _build_target_refs(
        targets=validation.targets,
        mention_labels_by_user_id=mention_labels_by_user_id,
        sender_id=context.sender_id,
        sender_full_name=context.sender_full_name,
    )
    for target in validation.targets:
        if not target.ok:
            target_ref = target_refs_by_zulip_id.get(target.zulip_user_id, f"user {target.zulip_user_id}")
            failures.append(f"{target_ref}: {target.message} ({target.code})")

    local_pr = _load_local_pr(owner=parsed.pr.owner, repo=parsed.pr.repo, number=parsed.pr.number)
    if local_pr is not None:
        if local_pr.state != PullRequestState.OPEN:
            failures.append("Pull request is not open in local sync data (state != open).")
            valid_targets = []
    elif _assignment_mutations_enabled():
        live_pr = _fetch_live_pr_view(owner=parsed.pr.owner, repo=parsed.pr.repo, number=parsed.pr.number, action=action)
        if live_pr is not None:
            if not live_pr.is_open:
                failures.append("Pull request is not open in GitHub live data.")
                valid_targets = []

    if not valid_targets:
        failures.append(f"No valid reviewers to {action} after validation.")
        return _summary_response(action=action, successes=successes, warnings=warnings, failures=failures)

    if not _assignment_mutations_enabled():
        github_logins = ", ".join(sorted({target.github_login for target in valid_targets if target.github_login}))
        successes.append(f"Validated targets: `{github_logins}`.")
        successes.append(f"Preflight passed for `{action}` on {parsed.pr.owner}/{parsed.pr.repo}#{parsed.pr.number}.")
        warnings.append("GitHub assignment mutation is disabled (enable ZULIP_ASSIGNMENT_MUTATIONS_ENABLED to execute).")
        return _summary_response(action=action, successes=successes, warnings=warnings, failures=failures)

    token = _assignment_token(action=action, owner=parsed.pr.owner, repo=parsed.pr.repo)
    if not token:
        failures.append("GitHub App token for assignment is not available for this repository/operation.")
        return _summary_response(action=action, successes=successes, warnings=warnings, failures=failures)

    gh_client = GitHubAssignmentClient(token=token)
    successful_logins: list[str] = []
    last_assignees_snapshot: tuple[str, ...] | None = None
    target_logins = _dedupe_target_logins(
        valid_targets=valid_targets,
        failures=failures,
        target_refs_by_zulip_id=target_refs_by_zulip_id,
    )
    if target_logins:
        try:
            last_assignees_snapshot = _run_assignment_mutation(
                gh_client=gh_client,
                action=action,
                owner=parsed.pr.owner,
                repo=parsed.pr.repo,
                number=parsed.pr.number,
                github_logins=target_logins,
            )
            successful_logins.extend(target_logins)
        except AssignmentMutationError as exc:
            if exc.code == "validation_failed" and len(target_logins) > 1:
                warnings.append("Batch mutation failed validation; retrying one reviewer at a time to isolate invalid targets.")
                for github_login in target_logins:
                    try:
                        last_assignees_snapshot = _run_assignment_mutation(
                            gh_client=gh_client,
                            action=action,
                            owner=parsed.pr.owner,
                            repo=parsed.pr.repo,
                            number=parsed.pr.number,
                            github_logins=(github_login,),
                        )
                        successful_logins.append(github_login)
                    except AssignmentMutationError as item_exc:
                        failures.append(f"{action} failed for `{github_login}`: {item_exc.message} ({item_exc.code})")
            else:
                failures.append(f"{action} failed for {_format_login_list(target_logins)}: {exc.message} ({exc.code})")

    if successful_logins:
        successes.append(f"{action} succeeded for {_format_login_list(tuple(successful_logins))}.")
        successes.append(_format_current_assignees(last_assignees_snapshot))
        _enqueue_post_action_sync(owner=parsed.pr.owner, repo=parsed.pr.repo, number=parsed.pr.number)

    return _summary_response(action=action, successes=successes, warnings=warnings, failures=failures)


def _summary_response(*, action: str, successes: list[str], warnings: list[str], failures: list[str]) -> CommandResult:
    del action  # action already reflected in entry lines
    if successes and not warnings and not failures:
        return CommandResult(content="\n".join(successes), response_mode=ResponseMode.PRIVATE)

    lines: list[str] = []
    if successes:
        lines.extend(successes)
    if warnings:
        if lines:
            lines.append("")
        lines.append("**Warnings:**")
        lines.extend(f"- {entry}" for entry in warnings)
    if failures:
        if lines:
            lines.append("")
        lines.append("**Failures:**")
        lines.extend(f"- {entry}" for entry in failures)
    return CommandResult(content="\n".join(lines), response_mode=ResponseMode.PRIVATE)


def _load_local_pr(*, owner: str, repo: str, number: int) -> PullRequest | None:
    return (
        PullRequest.objects.filter(repository__owner=owner, repository__name=repo, number=number)
        .only("id", "state", "assignees")
        .first()
    )


def _assignment_mutations_enabled() -> bool:
    value = str(getattr(settings, "ZULIP_ASSIGNMENT_MUTATIONS_ENABLED", "")).strip().lower()
    return value in {"1", "true", "yes", "on"}


def _assignment_token(*, action: str, owner: str, repo: str) -> str:
    return (
        resolve_github_app_operation_token(
            operation=_assignment_operation(action),
            owner=owner,
            repo=repo,
        )
        or ""
    )


def _assignment_operation(action: str) -> str | None:
    if action == "assign":
        return "assign_pr"
    if action == "unassign":
        return "unassign_pr"
    return None


def _fetch_live_pr_view(*, owner: str, repo: str, number: int, action: str) -> LivePullRequestView | None:
    token = _assignment_token(action=action, owner=owner, repo=repo)
    if not token:
        return None

    api_base_url = "https://api.github.com"
    url = f"{api_base_url}/repos/{owner}/{repo}/pulls/{number}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    try:
        response = requests.get(url, headers=headers, timeout=20)
    except requests.RequestException:
        return None

    if response.status_code >= 400:
        return None

    try:
        payload = response.json()
    except Exception:
        return None

    state = str(payload.get("state", "")).strip().lower()
    merged_at = payload.get("merged_at")
    is_open = state == "open" and not merged_at
    return LivePullRequestView(is_open=is_open)


def _run_assignment_mutation(
    *,
    gh_client: GitHubAssignmentClient,
    action: str,
    owner: str,
    repo: str,
    number: int,
    github_logins: tuple[str, ...],
) -> tuple[str, ...]:
    if action == "assign":
        return gh_client.assign_many(owner=owner, repo=repo, number=number, github_logins=github_logins)
    if action == "unassign":
        return gh_client.unassign_many(owner=owner, repo=repo, number=number, github_logins=github_logins)
    raise AssignmentMutationError(code="unsupported_action", message=f"Unsupported action `{action}`.")


def _dedupe_target_logins(
    *,
    valid_targets: list[AssignmentTargetValidation],
    failures: list[str],
    target_refs_by_zulip_id: dict[int, str],
) -> tuple[str, ...]:
    seen: set[str] = set()
    logins: list[str] = []
    for target in valid_targets:
        github_login = (target.github_login or "").strip()
        if not github_login:
            target_ref = target_refs_by_zulip_id.get(target.zulip_user_id, f"user {target.zulip_user_id}")
            failures.append(f"{target_ref}: missing GitHub login after validation.")
            continue
        login_lc = github_login.lower()
        if login_lc in seen:
            continue
        seen.add(login_lc)
        logins.append(github_login)
    return tuple(logins)


def _format_current_assignees(assignees: tuple[str, ...] | None) -> str:
    if assignees is None:
        return "Current assignees: unavailable (no successful GitHub mutation response snapshot)."
    if not assignees:
        return "Current assignees: none."
    rendered = ", ".join(f"`{login}`" for login in sorted(assignees, key=str.lower))
    return f"Current assignees: {rendered}."


def _format_login_list(logins: tuple[str, ...]) -> str:
    return ", ".join(f"`{login}`" for login in logins)


def _build_target_refs(
    *,
    targets: tuple[AssignmentTargetValidation, ...],
    mention_labels_by_user_id: dict[int, str],
    sender_id: int | None,
    sender_full_name: str | None,
) -> dict[int, str]:
    refs: dict[int, str] = {}
    for target in targets:
        label = (mention_labels_by_user_id.get(target.zulip_user_id) or "").strip()
        if not label and sender_id == target.zulip_user_id:
            label = (sender_full_name or "").strip()
        refs[target.zulip_user_id] = _format_silent_mention(zulip_user_id=target.zulip_user_id, label=label)
    return refs


def _format_silent_mention(*, zulip_user_id: int, label: str) -> str:
    clean_label = label.replace("|", " ").replace("*", "").strip() or "user"
    return f"@_**{clean_label}|{zulip_user_id}**"


def _enqueue_post_action_sync(*, owner: str, repo: str, number: int) -> None:
    repository = Repository.objects.filter(owner=owner, name=repo).only("id").first()
    if repository is None:
        return

    try:
        from syncer.tasks.sync_tasks import sync_pr_task

        sync_pr_task.delay(repository.id, int(number))
    except Exception:
        log.warning(
            "assignment_post_action_sync_enqueue_failed",
            extra={"owner": owner, "repo": repo, "number": number},
        )
