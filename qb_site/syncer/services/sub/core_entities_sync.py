from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from core.models.repository import Repository
from core.models.user import User


def upsert_repo_node_id(repo: Repository, repo_gid: Optional[str]) -> bool:
    """Persist GitHub repository node id onto a Repository row if provided.

    Returns True if the row was updated.
    """
    return upsert_repo_metadata(repo, repo_gid=repo_gid)[0]


def upsert_repo_metadata(
    repo: Repository,
    *,
    repo_gid: Optional[str] = None,
    owner_login: Optional[str] = None,
    name: Optional[str] = None,
    default_branch: Optional[str] = None,
    allow_rename: bool = False,
) -> tuple[bool, tuple[str, ...]]:
    """Update Repository identifiers and metadata from GitHub.

    - Updates github_node_id when provided.
    - Updates default_branch when provided.
    - Optionally updates owner/name if allow_rename=True and values differ.

    Returns: (changed_bool, updated_fields)
    """
    updated: list[str] = []
    if repo_gid and repo.github_node_id != repo_gid:
        repo.github_node_id = repo_gid
        updated.append("github_node_id")
    if default_branch and repo.default_branch != default_branch:
        repo.default_branch = default_branch
        updated.append("default_branch")
    if allow_rename:
        if owner_login and repo.owner != owner_login:
            repo.owner = owner_login
            updated.append("owner")
        if name and repo.name != name:
            repo.name = name
            updated.append("name")
    if updated:
        updated.append("updated_at")
        repo.save(update_fields=updated)
    return (len(updated) > 0, tuple(f for f in updated if f != "updated_at"))


def upsert_user_from_github(actor: Dict[str, Any] | None, *, create_missing: bool = True) -> Tuple[Optional[User], bool, Tuple[str, ...]]:
    """Create or update a core.User from a GitHub actor dict.

    Supports GraphQL shapes like:
      {"__typename": "User"|"Bot", "id": str | None, "login": str | None, "name": str | None, "avatarUrl": str | None}

    Resolution strategy
    - Prefer exact match by github_node_id when provided.
    - Fallback to case-insensitive match by github_login.
    - If not found and create_missing, create a new User when at least a login or node id is provided.

    Returns: (user_or_none, created_bool, updated_fields_tuple)
    """
    if not isinstance(actor, dict):
        return None, False, ()
    gid = actor.get("id")
    login = actor.get("login")
    name = actor.get("name")
    avatar = actor.get("avatarUrl")

    user: Optional[User] = None
    created = False
    updated_fields: list[str] = []

    if gid:
        user = User.objects.filter(github_node_id=gid).first()
        if user is None and login:
            user = User.objects.filter(github_login__iexact=login).first()
        if user is None and create_missing and (login or gid):
            user = User.objects.create(
                github_node_id=gid,
                github_login=login,
                name=name or None,
                avatar_url=avatar or None,
                is_active=True,
            )
            return user, True, ()
    elif login:
        user = User.objects.filter(github_login__iexact=login).first()
        if user is None and create_missing:
            user = User.objects.create(
                github_login=login,
                name=name or None,
                avatar_url=avatar or None,
                is_active=True,
            )
            return user, True, ()
    else:
        # no usable identity
        return None, False, ()

    # Update mutable fields if changed
    if user is None:
        return None, False, ()

    if gid and not user.github_node_id:
        user.github_node_id = gid
        updated_fields.append("github_node_id")
    if login and (user.github_login or "") != login:
        user.github_login = login
        updated_fields.append("github_login")
    if name is not None and (user.name or "") != (name or ""):
        user.name = name or None
        updated_fields.append("name")
    if avatar is not None and (user.avatar_url or "") != (avatar or ""):
        user.avatar_url = avatar or None
        updated_fields.append("avatar_url")
    if updated_fields:
        updated_fields.append("updated_at")
        user.save(update_fields=updated_fields)
    return user, created, tuple(f for f in updated_fields if f != "updated_at")
