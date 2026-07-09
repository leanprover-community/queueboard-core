"""Reviewer-session helpers for the console (design doc 050).

A thin wrapper over the Django session: the console stores the resolved ``core.User`` id after a
successful GitHub OAuth login and reads it back on each request. Kept separate from Django admin
auth (``request.user``) — a reviewer needs no Django account, only a GitHub identity.
"""

from __future__ import annotations

from django.http import HttpRequest

from core.models import User

SESSION_USER_KEY = "console_reviewer_user_id"
SESSION_NONCE_KEY = "console_oauth_nonce"


def set_reviewer(request: HttpRequest, user: User) -> None:
    # Rotate the session key on login promotion (like django.contrib.auth.login) so a
    # pre-authentication session key planted in the browser cannot be replayed (session fixation).
    request.session.cycle_key()
    request.session[SESSION_USER_KEY] = int(user.id)


def get_reviewer(request: HttpRequest) -> User | None:
    user_id = request.session.get(SESSION_USER_KEY)
    if not user_id:
        return None
    return User.objects.filter(id=int(user_id), is_active=True).first()


def clear_reviewer(request: HttpRequest) -> None:
    request.session.pop(SESSION_USER_KEY, None)


def set_oauth_nonce(request: HttpRequest, nonce: str) -> None:
    request.session[SESSION_NONCE_KEY] = nonce


def pop_oauth_nonce(request: HttpRequest) -> str | None:
    return request.session.pop(SESSION_NONCE_KEY, None)


__all__ = [
    "SESSION_USER_KEY",
    "SESSION_NONCE_KEY",
    "set_reviewer",
    "get_reviewer",
    "clear_reviewer",
    "set_oauth_nonce",
    "pop_oauth_nonce",
]
