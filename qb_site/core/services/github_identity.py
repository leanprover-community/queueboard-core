"""Resolve a GitHub OAuth identity to an existing ``core.User`` (Zulip-agnostic).

Used by the reviewer console (design doc 050): a reviewer authenticates with GitHub and we need
the matching ``core.User`` to key their proposals on. Unlike the registration linker this touches
no Zulip fields and **never creates users** — it only resolves people we already know (registered
via the Zulip flow, or ingested by the syncer) and refreshes their GitHub identity fields, so the
public console sign-in URL cannot mint a ``core.User`` row for an arbitrary GitHub account. If a
future caller needs create-on-miss, add it as an explicit separate entry point (the race-safe
savepoint pattern lives in ``zulip_bot.services.registration_linking`` and
``syncer...core_entities_sync.upsert_user_from_github``).
"""

from __future__ import annotations

from django.db import transaction

from core.models import User
from core.services.github_oauth import GitHubUserIdentity


@transaction.atomic
def resolve_user_from_identity(identity: GitHubUserIdentity) -> User | None:
    """Return the ``core.User`` for ``identity`` (by node id, else login), or ``None`` if unknown.

    A login match whose stored ``github_node_id`` is non-empty and differs from the identity's is
    a *recycled username* (the login now belongs to a different GitHub account) and is treated as
    no match — resolving it would hand the new account holder the previous owner's user (and, via
    the console, their session and proposals).

    Refreshes mutable identity fields (login/name/avatar) when they drift; never clobbers a
    conflicting non-empty ``github_node_id``.
    """
    user = User.objects.select_for_update().filter(github_node_id=identity.github_node_id).first()
    if user is None:
        user = User.objects.select_for_update().filter(github_login__iexact=identity.github_login).first()
        if user is not None and user.github_node_id and user.github_node_id != identity.github_node_id:
            return None
    if user is None:
        return None

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


__all__ = ["resolve_user_from_identity"]
