from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

from core.services.site_urls import build_site_url


@dataclass(frozen=True)
class RegistrationLinkClaims:
    zulip_user_id: int
    sender_email: str | None = None
    sender_full_name: str | None = None
    nonce: str | None = None
    iat: int | None = None
    exp: int | None = None


class RegistrationTokenError(Exception):
    pass


class RegistrationTokenExpired(RegistrationTokenError):
    pass


class RegistrationTokenInvalid(RegistrationTokenError):
    pass


def build_registration_link(*, claims: RegistrationLinkClaims) -> str:
    token = issue_registration_token(claims=claims)
    return build_site_url(f"/api/zulip/register/{quote(token, safe='')}/")


def issue_registration_token(*, claims: RegistrationLinkClaims) -> str:
    now_ts = int(time.time())
    payload = {
        "zulip_user_id": claims.zulip_user_id,
        "sender_email": claims.sender_email,
        "sender_full_name": claims.sender_full_name,
        "nonce": claims.nonce or secrets.token_urlsafe(16),
        "iat": now_ts,
        "exp": now_ts + _token_ttl_seconds(),
    }
    encrypted = _fernet().encrypt(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return encrypted.decode("utf-8")


def validate_registration_token(token: str) -> RegistrationLinkClaims:
    try:
        decrypted = _fernet().decrypt(token.encode("utf-8"))
    except (InvalidToken, ValueError, UnicodeEncodeError) as exc:
        raise RegistrationTokenInvalid("invalid token") from exc
    try:
        payload = json.loads(decrypted.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise RegistrationTokenInvalid("invalid payload") from exc
    return _parse_claims(payload)


def _parse_claims(payload: Any) -> RegistrationLinkClaims:
    if not isinstance(payload, dict):
        raise RegistrationTokenInvalid("invalid payload")

    zulip_user_id = payload.get("zulip_user_id")
    exp = payload.get("exp")
    nonce = payload.get("nonce")
    if not isinstance(zulip_user_id, int) or zulip_user_id <= 0:
        raise RegistrationTokenInvalid("invalid zulip_user_id")
    if not isinstance(exp, int):
        raise RegistrationTokenInvalid("invalid exp")
    if int(time.time()) > exp:
        raise RegistrationTokenExpired("token expired")
    if not isinstance(nonce, str) or not nonce:
        raise RegistrationTokenInvalid("invalid nonce")
    iat = payload.get("iat")
    if iat is not None and not isinstance(iat, int):
        raise RegistrationTokenInvalid("invalid iat")
    sender_email = payload.get("sender_email")
    if sender_email is not None and not isinstance(sender_email, str):
        raise RegistrationTokenInvalid("invalid sender_email")
    sender_full_name = payload.get("sender_full_name")
    if sender_full_name is not None and not isinstance(sender_full_name, str):
        raise RegistrationTokenInvalid("invalid sender_full_name")
    return RegistrationLinkClaims(
        zulip_user_id=zulip_user_id,
        sender_email=sender_email,
        sender_full_name=sender_full_name,
        nonce=nonce,
        iat=iat,
        exp=exp,
    )


def _token_secret() -> str:
    prefs_secret = getattr(settings, "ZULIP_PREFS_TOKEN_SECRET", "").strip()
    if prefs_secret:
        return prefs_secret
    return settings.SECRET_KEY


def _token_salt() -> str:
    return getattr(settings, "ZULIP_REGISTRATION_TOKEN_SALT", "zulip_bot.registration")


def _fernet() -> Fernet:
    material = f"{_token_secret()}:{_token_salt()}".encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(material).digest())
    return Fernet(key)


def _token_ttl_seconds() -> int:
    return int(getattr(settings, "ZULIP_REGISTRATION_TOKEN_TTL_SECONDS", 1800))
