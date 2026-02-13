from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


@dataclass(frozen=True)
class PrefsLinkClaims:
    user_id: int
    zulip_user_id: int
    preference_ids: tuple[int, ...]
    iat: int | None = None
    exp: int | None = None


class PrefsTokenError(Exception):
    pass


class PrefsTokenExpired(PrefsTokenError):
    pass


class PrefsTokenInvalid(PrefsTokenError):
    pass


def build_prefs_link(*, claims: PrefsLinkClaims) -> str:
    token = issue_prefs_token(claims=claims)
    url_base = getattr(settings, "ZULIP_PREFS_URL_BASE", "").strip().rstrip("/")
    path = f"/api/zulip/prefs/{quote(token, safe='')}/"
    if url_base:
        return f"{url_base}{path}"
    return path


def issue_prefs_token(*, claims: PrefsLinkClaims) -> str:
    now_ts = int(time.time())
    payload = {
        "user_id": claims.user_id,
        "zulip_user_id": claims.zulip_user_id,
        "preference_ids": list(claims.preference_ids),
        "iat": now_ts,
        "exp": now_ts + _token_ttl_seconds(),
    }
    encrypted = _fernet().encrypt(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return encrypted.decode("utf-8")


def validate_prefs_token(token: str) -> PrefsLinkClaims:
    try:
        decrypted = _fernet().decrypt(token.encode("utf-8"))
    except (InvalidToken, ValueError, UnicodeEncodeError) as exc:
        raise PrefsTokenInvalid("invalid token") from exc
    try:
        payload = json.loads(decrypted.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise PrefsTokenInvalid("invalid payload") from exc
    return _parse_claims(payload)


def _parse_claims(payload: Any) -> PrefsLinkClaims:
    if not isinstance(payload, dict):
        raise PrefsTokenInvalid("invalid payload")

    user_id = payload.get("user_id")
    zulip_user_id = payload.get("zulip_user_id")
    preference_ids = payload.get("preference_ids")
    exp = payload.get("exp")
    if not isinstance(user_id, int) or user_id <= 0:
        raise PrefsTokenInvalid("invalid user_id")
    if not isinstance(zulip_user_id, int) or zulip_user_id <= 0:
        raise PrefsTokenInvalid("invalid zulip_user_id")
    if not isinstance(preference_ids, list) or not all(isinstance(value, int) and value > 0 for value in preference_ids):
        raise PrefsTokenInvalid("invalid preference_ids")
    if not isinstance(exp, int):
        raise PrefsTokenInvalid("invalid exp")
    if int(time.time()) > exp:
        raise PrefsTokenExpired("token expired")
    iat = payload.get("iat")
    if iat is not None and not isinstance(iat, int):
        raise PrefsTokenInvalid("invalid iat")
    return PrefsLinkClaims(
        user_id=user_id,
        zulip_user_id=zulip_user_id,
        preference_ids=tuple(preference_ids),
        iat=iat,
        exp=exp,
    )


def _token_secret() -> str:
    custom = getattr(settings, "ZULIP_PREFS_TOKEN_SECRET", "").strip()
    if custom:
        return custom
    return settings.SECRET_KEY


def _token_salt() -> str:
    return getattr(settings, "ZULIP_PREFS_TOKEN_SALT", "zulip_bot.prefs")


def _fernet() -> Fernet:
    material = f"{_token_secret()}:{_token_salt()}".encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(material).digest())
    return Fernet(key)


def _token_ttl_seconds() -> int:
    return int(getattr(settings, "ZULIP_PREFS_TOKEN_TTL_SECONDS", 1800))
