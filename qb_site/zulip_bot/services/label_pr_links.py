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
class LabelPRLinkClaims:
    zulip_user_id: int
    github_login: str
    pr_owner: str
    pr_repo: str
    pr_number: int
    iat: int | None = None
    exp: int | None = None


class LabelPRTokenError(Exception):
    pass


class LabelPRTokenExpired(LabelPRTokenError):
    pass


class LabelPRTokenInvalid(LabelPRTokenError):
    pass


def build_label_pr_link(*, claims: LabelPRLinkClaims) -> str:
    token = issue_label_pr_token(claims=claims)
    url_base = getattr(settings, "ZULIP_PREFS_URL_BASE", "").strip().rstrip("/")
    path = f"/api/zulip/label-pr/{quote(token, safe='')}/"
    if url_base:
        return f"{url_base}{path}"
    return path


def issue_label_pr_token(*, claims: LabelPRLinkClaims) -> str:
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


def validate_label_pr_token(token: str) -> LabelPRLinkClaims:
    try:
        decrypted = _fernet().decrypt(token.encode("utf-8"))
    except (InvalidToken, ValueError, UnicodeEncodeError) as exc:
        raise LabelPRTokenInvalid("invalid token") from exc
    try:
        payload = json.loads(decrypted.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise LabelPRTokenInvalid("invalid payload") from exc
    return _parse_claims(payload)


def _parse_claims(payload: Any) -> LabelPRLinkClaims:
    if not isinstance(payload, dict):
        raise LabelPRTokenInvalid("invalid payload")

    zulip_user_id = payload.get("zulip_user_id")
    github_login = payload.get("github_login")
    pr_owner = payload.get("pr_owner")
    pr_repo = payload.get("pr_repo")
    pr_number = payload.get("pr_number")
    exp = payload.get("exp")

    if not isinstance(zulip_user_id, int) or zulip_user_id <= 0:
        raise LabelPRTokenInvalid("invalid zulip_user_id")
    if not isinstance(github_login, str) or not github_login.strip():
        raise LabelPRTokenInvalid("invalid github_login")
    if not isinstance(pr_owner, str) or not pr_owner.strip():
        raise LabelPRTokenInvalid("invalid pr_owner")
    if not isinstance(pr_repo, str) or not pr_repo.strip():
        raise LabelPRTokenInvalid("invalid pr_repo")
    if not isinstance(pr_number, int) or pr_number <= 0:
        raise LabelPRTokenInvalid("invalid pr_number")
    if not isinstance(exp, int):
        raise LabelPRTokenInvalid("invalid exp")
    if int(time.time()) > exp:
        raise LabelPRTokenExpired("token expired")

    iat = payload.get("iat")
    if iat is not None and not isinstance(iat, int):
        raise LabelPRTokenInvalid("invalid iat")

    return LabelPRLinkClaims(
        zulip_user_id=zulip_user_id,
        github_login=github_login,
        pr_owner=pr_owner,
        pr_repo=pr_repo,
        pr_number=pr_number,
        iat=iat,
        exp=exp,
    )


def _token_secret() -> str:
    custom = getattr(settings, "ZULIP_LABEL_PR_TOKEN_SECRET", "").strip()
    if custom:
        return custom
    return settings.SECRET_KEY


def _token_salt() -> str:
    return getattr(settings, "ZULIP_LABEL_PR_TOKEN_SALT", "zulip_bot.label_pr")


def _fernet() -> Fernet:
    material = f"{_token_secret()}:{_token_salt()}".encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(material).digest())
    return Fernet(key)


def _token_ttl_seconds() -> int:
    return int(getattr(settings, "ZULIP_LABEL_PR_TOKEN_TTL_SECONDS", 1800))
