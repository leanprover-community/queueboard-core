from __future__ import annotations

from dataclasses import dataclass

from core.models import Repository, ReviewerPreference, User


@dataclass(frozen=True)
class PreferenceBootstrapResult:
    active_repository_count: int
    created_count: int
    existing_count: int


def ensure_default_preferences_for_user(*, user: User) -> PreferenceBootstrapResult:
    active_repo_ids = list(Repository.objects.filter(is_active=True).values_list("id", flat=True).order_by("id"))
    if not active_repo_ids:
        return PreferenceBootstrapResult(
            active_repository_count=0,
            created_count=0,
            existing_count=0,
        )

    before_existing = set(
        ReviewerPreference.objects.filter(user_id=user.id, repository_id__in=active_repo_ids).values_list(
            "repository_id", flat=True
        )
    )
    missing_repo_ids = [repo_id for repo_id in active_repo_ids if repo_id not in before_existing]
    if missing_repo_ids:
        ReviewerPreference.objects.bulk_create(
            [ReviewerPreference(user_id=user.id, repository_id=repo_id) for repo_id in missing_repo_ids],
            ignore_conflicts=True,
        )
    after_count = ReviewerPreference.objects.filter(user_id=user.id, repository_id__in=active_repo_ids).count()
    before_count = len(before_existing)
    created_count = max(0, after_count - before_count)
    existing_count = after_count - created_count
    return PreferenceBootstrapResult(
        active_repository_count=len(active_repo_ids),
        created_count=created_count,
        existing_count=existing_count,
    )
