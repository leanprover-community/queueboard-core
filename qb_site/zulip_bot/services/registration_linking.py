from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction

from core.models import User
from core.services.github_oauth import GitHubUserIdentity


@dataclass(frozen=True)
class RegistrationLinkConflict(RuntimeError):
    reason: str
    message: str

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class RegistrationLinkResult:
    user: User
    outcome: str


@transaction.atomic
def link_or_create_user_from_registration(
    *,
    zulip_user_id: int,
    zulip_full_name: str | None,
    identity: GitHubUserIdentity,
) -> RegistrationLinkResult:
    user = User.objects.select_for_update().filter(github_node_id=identity.github_node_id).first()
    if user is None:
        user = User.objects.select_for_update().filter(github_login__iexact=identity.github_login).first()
        if user and user.github_node_id and user.github_node_id != identity.github_node_id:
            raise RegistrationLinkConflict(
                reason="github_login_bound_to_different_node_id",
                message="This GitHub login is already associated with a different GitHub account in Queueboard.",
            )

    if user is None:
        created = User.objects.create(
            github_node_id=identity.github_node_id,
            github_login=identity.github_login,
            name=identity.github_name,
            avatar_url=identity.github_avatar_url,
            zulip_user_id=zulip_user_id,
            zulip_full_name=zulip_full_name,
            is_active=True,
        )
        return RegistrationLinkResult(user=created, outcome="created")

    if user.zulip_user_id not in (None, zulip_user_id):
        raise RegistrationLinkConflict(
            reason="github_account_already_linked_to_other_zulip",
            message="This GitHub account is already linked to a different Zulip user.",
        )

    outcome = "already_linked" if user.zulip_user_id == zulip_user_id else "linked_existing"
    changed_fields: set[str] = set()
    if user.github_node_id != identity.github_node_id:
        user.github_node_id = identity.github_node_id
        changed_fields.add("github_node_id")
    if user.github_login != identity.github_login:
        user.github_login = identity.github_login
        changed_fields.add("github_login")
    if user.name != identity.github_name:
        user.name = identity.github_name
        changed_fields.add("name")
    if user.avatar_url != identity.github_avatar_url:
        user.avatar_url = identity.github_avatar_url
        changed_fields.add("avatar_url")
    if user.zulip_user_id != zulip_user_id:
        user.zulip_user_id = zulip_user_id
        changed_fields.add("zulip_user_id")
    if zulip_full_name is not None and user.zulip_full_name != zulip_full_name:
        user.zulip_full_name = zulip_full_name
        changed_fields.add("zulip_full_name")
    if changed_fields:
        user.save(update_fields=sorted(changed_fields))

    return RegistrationLinkResult(user=user, outcome=outcome)
