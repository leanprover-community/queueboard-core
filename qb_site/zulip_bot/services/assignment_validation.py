from __future__ import annotations

from dataclasses import dataclass

from core.models import Repository, ReviewerPreference, User
from zulip_bot.services.assignment_command_parser import GitHubPullRequestRef


@dataclass(frozen=True)
class AssignmentTargetValidation:
    zulip_user_id: int
    ok: bool
    code: str
    message: str
    user_id: int | None = None
    github_login: str | None = None


@dataclass(frozen=True)
class AssignmentValidationResult:
    repository: Repository | None
    targets: tuple[AssignmentTargetValidation, ...]


def validate_assignment_targets(*, pr: GitHubPullRequestRef, target_user_ids: tuple[int, ...]) -> AssignmentValidationResult:
    repository = Repository.objects.filter(owner=pr.owner, name=pr.repo).only("id", "owner", "name").first()
    users_by_zulip_id = {
        int(user.zulip_user_id): user
        for user in User.objects.filter(zulip_user_id__in=target_user_ids).only("id", "zulip_user_id", "github_login")
        if user.zulip_user_id is not None
    }

    pref_user_ids: set[int] = set()
    if repository is not None:
        pref_user_ids = set(
            ReviewerPreference.objects.filter(
                repository_id=repository.id,
                user_id__in=[user.id for user in users_by_zulip_id.values()],
            ).values_list("user_id", flat=True)
        )

    results: list[AssignmentTargetValidation] = []
    for target_id in target_user_ids:
        user = users_by_zulip_id.get(target_id)
        if user is None:
            results.append(
                AssignmentTargetValidation(
                    zulip_user_id=target_id,
                    ok=False,
                    code="unknown_reviewer",
                    message="No Queueboard user is linked to that Zulip user id.",
                )
            )
            continue

        github_login = (user.github_login or "").strip()
        if not github_login:
            results.append(
                AssignmentTargetValidation(
                    zulip_user_id=target_id,
                    ok=False,
                    code="missing_github_login",
                    message="Reviewer is linked to Zulip but does not have a GitHub login set.",
                    user_id=user.id,
                )
            )
            continue

        if repository is None:
            results.append(
                AssignmentTargetValidation(
                    zulip_user_id=target_id,
                    ok=False,
                    code="repository_not_configured",
                    message=f"Repository {pr.owner}/{pr.repo} is not configured in Queueboard.",
                    user_id=user.id,
                    github_login=github_login,
                )
            )
            continue

        if user.id not in pref_user_ids:
            results.append(
                AssignmentTargetValidation(
                    zulip_user_id=target_id,
                    ok=False,
                    code="missing_preference",
                    message=f"Reviewer has no ReviewerPreference for {pr.owner}/{pr.repo}.",
                    user_id=user.id,
                    github_login=github_login,
                )
            )
            continue

        results.append(
            AssignmentTargetValidation(
                zulip_user_id=target_id,
                ok=True,
                code="ok",
                message="ok",
                user_id=user.id,
                github_login=github_login,
            )
        )

    return AssignmentValidationResult(repository=repository, targets=tuple(results))
