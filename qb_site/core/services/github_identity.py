"""Resolve a GitHub OAuth identity to a ``core.User`` (Zulip-agnostic).

Used by the reviewer console (design doc 050): a reviewer authenticates with GitHub and we need
the matching ``core.User`` to key their proposals on. Unlike the registration linker this touches
no Zulip fields — it only resolves/creates the person and refreshes their GitHub identity fields.
The console calls it with ``create=False`` so it gates on already-known people rather than minting
a row for any GitHub account that signs in.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction

from core.models import User
from core.services.github_oauth import GitHubUserIdentity


def _resolve_existing(identity: GitHubUserIdentity) -> User | None:
    """Look up an existing user by node id, else by login (rejecting recycled logins)."""
    user = User.objects.select_for_update().filter(github_node_id=identity.github_node_id).first()
    if user is not None:
        return user
    user = User.objects.select_for_update().filter(github_login__iexact=identity.github_login).first()
    if user is not None and user.github_node_id and user.github_node_id != identity.github_node_id:
        return None
    return user


@transaction.atomic
def resolve_or_create_user_from_identity(identity: GitHubUserIdentity, *, create: bool = True) -> User | None:
    """Return the ``core.User`` for ``identity`` (by node id, else login).

    With ``create=True`` (the default) a matching user is created when none exists. With
    ``create=False`` the lookup is resolve-only: it returns ``None`` for an identity we have never
    seen, so a caller like the reviewer console can gate access to already-known people (registered
    via the Zulip flow, or ingested by the syncer) instead of minting a row for any GitHub account
    that completes OAuth.

    Race-safe against the syncer ingesting the same GitHub user concurrently (savepoint + re-fetch,
    per the "Concurrent Writers and Unique Keys" rules). Refreshes mutable identity fields
    (login/name/avatar) when they drift; never clobbers a conflicting non-empty ``github_node_id``.

    A login match whose stored ``github_node_id`` is non-empty and differs from the identity's is a
    *recycled username* (the login now belongs to a different GitHub account) and is treated as no
    match — resolving it would hand the new account holder the previous owner's user (and, via the
    console, their session and proposals).
    """
    user = _resolve_existing(identity)

    if user is None:
        if not create:
            return None
        try:
            with transaction.atomic():
                return User.objects.create(
                    github_node_id=identity.github_node_id,
                    github_login=identity.github_login,
                    name=identity.github_name,
                    avatar_url=identity.github_avatar_url,
                    is_active=True,
                )
        except IntegrityError:
            user = _resolve_existing(identity)
            if user is None:
                raise

    changed: set[str] = set()
    if not user.github_node_id and identity.github_node_id:
        user.github_node_id = identity.github_node_id
        changed.add("github_node_id")
    if user.github_login != identity.github_login:
        user.github_login = identity.github_login
        changed.add("github_login")
    if user.name != identity.github_name:
        user.name = identity.github_name
        changed.add("name")
    if user.avatar_url != identity.github_avatar_url:
        user.avatar_url = identity.github_avatar_url
        changed.add("avatar_url")
    if changed:
        user.save(update_fields=sorted(changed))
    return user


__all__ = ["resolve_or_create_user_from_identity"]
