from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings

from core.services.oauth_state import (
    SignedStateExpired,
    SignedStateInvalid,
    issue_signed_state,
    read_signed_state,
)


@dataclass(frozen=True)
class RegistrationOAuthStateClaims:
    registration_token: str
    registration_nonce: str
    iat: int | None = None
    exp: int | None = None


class RegistrationOAuthStateError(Exception):
    pass


class RegistrationOAuthStateExpired(RegistrationOAuthStateError):
    pass


class RegistrationOAuthStateInvalid(RegistrationOAuthStateError):
    pass


def issue_registration_oauth_state(*, claims: RegistrationOAuthStateClaims) -> str:
    return issue_signed_state(
        {
            "registration_token": claims.registration_token,
            "registration_nonce": claims.registration_nonce,
        },
        secret=_token_secret(),
        salt=_token_salt(),
        ttl_seconds=_state_ttl_seconds(),
    )


def validate_registration_oauth_state(state: str) -> RegistrationOAuthStateClaims:
    try:
        payload = read_signed_state(state, secret=_token_secret(), salt=_token_salt())
    except SignedStateExpired as exc:
        raise RegistrationOAuthStateExpired("state expired") from exc
    except SignedStateInvalid as exc:
        raise RegistrationOAuthStateInvalid("invalid state") from exc
    registration_token = payload.get("registration_token")
    registration_nonce = payload.get("registration_nonce")
    if not isinstance(registration_token, str) or not registration_token:
        raise RegistrationOAuthStateInvalid("invalid registration_token")
    if not isinstance(registration_nonce, str) or not registration_nonce:
        raise RegistrationOAuthStateInvalid("invalid registration_nonce")
    iat = payload.get("iat")
    if iat is not None and not isinstance(iat, int):
        raise RegistrationOAuthStateInvalid("invalid iat")
    return RegistrationOAuthStateClaims(
        registration_token=registration_token,
        registration_nonce=registration_nonce,
        iat=iat,
        exp=payload.get("exp"),
    )


def _token_secret() -> str:
    prefs_secret = getattr(settings, "ZULIP_PREFS_TOKEN_SECRET", "").strip()
    if prefs_secret:
        return prefs_secret
    return settings.SECRET_KEY


def _token_salt() -> str:
    return getattr(settings, "ZULIP_REGISTRATION_OAUTH_STATE_SALT", "zulip_bot.registration.oauth_state")


def _state_ttl_seconds() -> int:
    return int(getattr(settings, "ZULIP_REGISTRATION_OAUTH_STATE_TTL_SECONDS", 600))
