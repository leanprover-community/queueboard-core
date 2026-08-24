"""The self-serve registration link (design doc 025).

Unlike the per-PR action links, this one authenticates someone who has *no account yet*, so it stays
a bearer secret in a URL by necessity — there is no session to fall back on. Built on the shared
`core.services.signed_payloads` primitive; the `nonce` is echoed in the OAuth `state`
(`registration_oauth_state`) and re-checked on callback, which is why both share
`ZULIP_LINK_TOKEN_SECRET`.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from urllib.parse import quote

from django.conf import settings

from core.services.signed_payloads import (
    SignedPayloadExpired,
    SignedPayloadInvalid,
    issue_signed_payload,
    read_signed_payload,
)
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


def build_registration_link(*, claims: RegistrationLinkClaims, now: int | None = None) -> str:
    token = issue_registration_token(claims=claims, now=now)
    return build_site_url(f"/api/zulip/register/{quote(token, safe='')}/")


def issue_registration_token(*, claims: RegistrationLinkClaims, now: int | None = None) -> str:
    return issue_signed_payload(
        {
            "zulip_user_id": claims.zulip_user_id,
            "sender_email": claims.sender_email,
            "sender_full_name": claims.sender_full_name,
            "nonce": claims.nonce or secrets.token_urlsafe(16),
        },
        secret=_token_secret(),
        salt=_token_salt(),
        ttl_seconds=_token_ttl_seconds(),
        now=now,
    )


def validate_registration_token(token: str, *, now: int | None = None) -> RegistrationLinkClaims:
    try:
        payload = read_signed_payload(token, secret=_token_secret(), salt=_token_salt(), now=now)
    except SignedPayloadExpired as exc:
        raise RegistrationTokenExpired("token expired") from exc
    except SignedPayloadInvalid as exc:
        raise RegistrationTokenInvalid("invalid token") from exc

    zulip_user_id = payload.get("zulip_user_id")
    nonce = payload.get("nonce")
    if not isinstance(zulip_user_id, int) or zulip_user_id <= 0:
        raise RegistrationTokenInvalid("invalid zulip_user_id")
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
        exp=payload.get("exp"),
    )


def _token_secret() -> str:
    # Shared with the registration OAuth state; see ZULIP_LINK_TOKEN_SECRET in settings/base.py, which
    # also honors the legacy ZULIP_PREFS_TOKEN_SECRET env name.
    custom = getattr(settings, "ZULIP_LINK_TOKEN_SECRET", "").strip()
    if custom:
        return custom
    return settings.SECRET_KEY


def _token_salt() -> str:
    return getattr(settings, "ZULIP_REGISTRATION_TOKEN_SALT", "zulip_bot.registration")


def _token_ttl_seconds() -> int:
    return int(getattr(settings, "ZULIP_REGISTRATION_TOKEN_TTL_SECONDS", 1800))
