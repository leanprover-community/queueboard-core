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
class ClosePRLinkClaims:
    zulip_user_id: int
    github_login: str
    pr_owner: str
    pr_repo: str
    pr_number: int
    iat: int | None = None
    exp: int | None = None


class ClosePRTokenError(Exception):
    pass


class ClosePRTokenExpired(ClosePRTokenError):
    pass


class ClosePRTokenInvalid(ClosePRTokenError):
    pass


def build_close_pr_link(*, claims: ClosePRLinkClaims) -> str:
    token = issue_close_pr_token(claims=claims)
    url_base = getattr(settings, "ZULIP_PREFS_URL_BASE", "").strip().rstrip("/")
    path = f"/api/zulip/close-pr/{quote(token, safe='')}/"
    if url_base:
        return f"{url_base}{path}"
    return path


def issue_close_pr_token(*, claims: ClosePRLinkClaims) -> str:
    now_ts = int(time.time())
    payload = {
        "zulip_user_id": claims.zulip_user_id,
        "github_login": claims.github_login,
        "pr_owner": claims.pr_owner,
        "pr_repo": claims.pr_repo,
        "pr_number": claims.pr_number,
        "iat": now_ts,
        "exp": now_ts + _token_ttl_seconds(),
    }
    encrypted = _fernet().encrypt(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return encrypted.decode("utf-8")


def validate_close_pr_token(token: str) -> ClosePRLinkClaims:
    try:
        decrypted = _fernet().decrypt(token.encode("utf-8"))
    except (InvalidToken, ValueError, UnicodeEncodeError) as exc:
        raise ClosePRTokenInvalid("invalid token") from exc
    try:
        payload = json.loads(decrypted.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ClosePRTokenInvalid("invalid payload") from exc
    return _parse_claims(payload)


def _parse_claims(payload: Any) -> ClosePRLinkClaims:
    if not isinstance(payload, dict):
        raise ClosePRTokenInvalid("invalid payload")

    zulip_user_id = payload.get("zulip_user_id")
    github_login = payload.get("github_login")
    pr_owner = payload.get("pr_owner")
    pr_repo = payload.get("pr_repo")
    pr_number = payload.get("pr_number")
    exp = payload.get("exp")

    if not isinstance(zulip_user_id, int) or zulip_user_id <= 0:
        raise ClosePRTokenInvalid("invalid zulip_user_id")
    if not isinstance(github_login, str) or not github_login.strip():
        raise ClosePRTokenInvalid("invalid github_login")
    if not isinstance(pr_owner, str) or not pr_owner.strip():
        raise ClosePRTokenInvalid("invalid pr_owner")
    if not isinstance(pr_repo, str) or not pr_repo.strip():
        raise ClosePRTokenInvalid("invalid pr_repo")
    if not isinstance(pr_number, int) or pr_number <= 0:
        raise ClosePRTokenInvalid("invalid pr_number")
    if not isinstance(exp, int):
        raise ClosePRTokenInvalid("invalid exp")
    if int(time.time()) > exp:
        raise ClosePRTokenExpired("token expired")

    iat = payload.get("iat")
    if iat is not None and not isinstance(iat, int):
        raise ClosePRTokenInvalid("invalid iat")

    return ClosePRLinkClaims(
        zulip_user_id=zulip_user_id,
        github_login=github_login,
        pr_owner=pr_owner,
        pr_repo=pr_repo,
        pr_number=pr_number,
        iat=iat,
        exp=exp,
    )


def _token_secret() -> str:
    custom = getattr(settings, "ZULIP_CLOSE_PR_TOKEN_SECRET", "").strip()
    if custom:
        return custom
    return settings.SECRET_KEY


def _token_salt() -> str:
    return getattr(settings, "ZULIP_CLOSE_PR_TOKEN_SALT", "zulip_bot.close_pr")


def _fernet() -> Fernet:
    material = f"{_token_secret()}:{_token_salt()}".encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(material).digest())
    return Fernet(key)


def _token_ttl_seconds() -> int:
    return int(getattr(settings, "ZULIP_CLOSE_PR_TOKEN_TTL_SECONDS", 1800))
