from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


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
    now_ts = int(time.time())
    payload = {
        "registration_token": claims.registration_token,
        "registration_nonce": claims.registration_nonce,
        "iat": now_ts,
        "exp": now_ts + _state_ttl_seconds(),
    }
    encrypted = _fernet().encrypt(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return encrypted.decode("utf-8")


def validate_registration_oauth_state(state: str) -> RegistrationOAuthStateClaims:
    try:
        decrypted = _fernet().decrypt(state.encode("utf-8"))
    except (InvalidToken, ValueError, UnicodeEncodeError) as exc:
        raise RegistrationOAuthStateInvalid("invalid state") from exc
    try:
        payload = json.loads(decrypted.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise RegistrationOAuthStateInvalid("invalid payload") from exc
    return _parse_claims(payload)


def _parse_claims(payload: Any) -> RegistrationOAuthStateClaims:
    if not isinstance(payload, dict):
        raise RegistrationOAuthStateInvalid("invalid payload")
    registration_token = payload.get("registration_token")
    registration_nonce = payload.get("registration_nonce")
    exp = payload.get("exp")
    if not isinstance(registration_token, str) or not registration_token:
        raise RegistrationOAuthStateInvalid("invalid registration_token")
    if not isinstance(registration_nonce, str) or not registration_nonce:
        raise RegistrationOAuthStateInvalid("invalid registration_nonce")
    if not isinstance(exp, int):
        raise RegistrationOAuthStateInvalid("invalid exp")
    if int(time.time()) > exp:
        raise RegistrationOAuthStateExpired("state expired")
    iat = payload.get("iat")
    if iat is not None and not isinstance(iat, int):
        raise RegistrationOAuthStateInvalid("invalid iat")
    return RegistrationOAuthStateClaims(
        registration_token=registration_token,
        registration_nonce=registration_nonce,
        iat=iat,
        exp=exp,
    )


def _token_secret() -> str:
    prefs_secret = getattr(settings, "ZULIP_PREFS_TOKEN_SECRET", "").strip()
    if prefs_secret:
        return prefs_secret
    return settings.SECRET_KEY


def _token_salt() -> str:
    return getattr(settings, "ZULIP_REGISTRATION_OAUTH_STATE_SALT", "zulip_bot.registration.oauth_state")


def _fernet() -> Fernet:
    material = f"{_token_secret()}:{_token_salt()}".encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(material).digest())
    return Fernet(key)


def _state_ttl_seconds() -> int:
    return int(getattr(settings, "ZULIP_REGISTRATION_OAUTH_STATE_TTL_SECONDS", 600))
