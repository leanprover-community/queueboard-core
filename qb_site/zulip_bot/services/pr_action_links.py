"""Signed, expiring links to the per-PR action forms — `close-pr` (doc 041) and `label-pr` (doc 042).

One module, two actions. The claims payload, validation and link shape are identical; the actions
differ only in **signing secret, salt, TTL and URL path**. Those stay per-action deliberately:

- the settings are documented in `.env.example` and a deployment may have set them, and
- keeping the salts distinct means a `close-pr` token can never validate as a `label-pr` one, which
  matters because the two grant different GitHub mutations.

So this is wire-compatible with the two byte-identical modules it replaced (`close_pr_links.py`,
`label_pr_links.py`): tokens issued before the consolidation still validate.

`ttl_seconds()` is exported because the commands quote the expiry time in their DM. Reading the TTL
from here rather than re-`getattr`-ing the setting keeps the DM's claim and the token's real `exp`
from drifting apart.

See design doc 052 for why these links are *not* simply migrated to the console session the way the
prefs form was: the token also carries the target PR and a permission decision already made, and the
audiences (PR author, write-access collaborator) do not match the console's reviewer-only admission.
"""

from __future__ import annotations

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
class PRActionLinkClaims:
    zulip_user_id: int
    github_login: str
    pr_owner: str
    pr_repo: str
    pr_number: int
    iat: int | None = None
    exp: int | None = None


class PRActionTokenError(Exception):
    pass


class PRActionTokenExpired(PRActionTokenError):
    pass


class PRActionTokenInvalid(PRActionTokenError):
    pass


@dataclass(frozen=True)
class PRAction:
    """The per-action knobs. Values must not change: they are the wire format."""

    name: str
    url_path: str
    secret_setting: str
    salt_setting: str
    salt_default: str
    ttl_setting: str
    ttl_default: int = 1800


CLOSE_PR = PRAction(
    name="close_pr",
    url_path="close-pr",
    secret_setting="ZULIP_CLOSE_PR_TOKEN_SECRET",
    salt_setting="ZULIP_CLOSE_PR_TOKEN_SALT",
    salt_default="zulip_bot.close_pr",
    ttl_setting="ZULIP_CLOSE_PR_TOKEN_TTL_SECONDS",
)

LABEL_PR = PRAction(
    name="label_pr",
    url_path="label-pr",
    secret_setting="ZULIP_LABEL_PR_TOKEN_SECRET",
    salt_setting="ZULIP_LABEL_PR_TOKEN_SALT",
    salt_default="zulip_bot.label_pr",
    ttl_setting="ZULIP_LABEL_PR_TOKEN_TTL_SECONDS",
)


def build_pr_action_link(*, action: PRAction, claims: PRActionLinkClaims, now: int | None = None) -> str:
    token = issue_pr_action_token(action=action, claims=claims, now=now)
    return build_site_url(f"/api/zulip/{action.url_path}/{quote(token, safe='')}/")


def issue_pr_action_token(*, action: PRAction, claims: PRActionLinkClaims, now: int | None = None) -> str:
    return issue_signed_payload(
        {
            "zulip_user_id": claims.zulip_user_id,
            "github_login": claims.github_login,
            "pr_owner": claims.pr_owner,
            "pr_repo": claims.pr_repo,
            "pr_number": claims.pr_number,
        },
        secret=_token_secret(action),
        salt=_token_salt(action),
        ttl_seconds=ttl_seconds(action),
        now=now,
    )


def validate_pr_action_token(token: str, *, action: PRAction, now: int | None = None) -> PRActionLinkClaims:
    try:
        payload = read_signed_payload(token, secret=_token_secret(action), salt=_token_salt(action), now=now)
    except SignedPayloadExpired as exc:
        raise PRActionTokenExpired("token expired") from exc
    except SignedPayloadInvalid as exc:
        raise PRActionTokenInvalid("invalid token") from exc

    zulip_user_id = payload.get("zulip_user_id")
    github_login = payload.get("github_login")
    pr_owner = payload.get("pr_owner")
    pr_repo = payload.get("pr_repo")
    pr_number = payload.get("pr_number")

    if not isinstance(zulip_user_id, int) or zulip_user_id <= 0:
        raise PRActionTokenInvalid("invalid zulip_user_id")
    if not isinstance(github_login, str) or not github_login.strip():
        raise PRActionTokenInvalid("invalid github_login")
    if not isinstance(pr_owner, str) or not pr_owner.strip():
        raise PRActionTokenInvalid("invalid pr_owner")
    if not isinstance(pr_repo, str) or not pr_repo.strip():
        raise PRActionTokenInvalid("invalid pr_repo")
    if not isinstance(pr_number, int) or pr_number <= 0:
        raise PRActionTokenInvalid("invalid pr_number")

    iat = payload.get("iat")
    if iat is not None and not isinstance(iat, int):
        raise PRActionTokenInvalid("invalid iat")

    return PRActionLinkClaims(
        zulip_user_id=zulip_user_id,
        github_login=github_login,
        pr_owner=pr_owner,
        pr_repo=pr_repo,
        pr_number=pr_number,
        iat=iat,
        exp=payload.get("exp"),
    )


def ttl_seconds(action: PRAction) -> int:
    return int(getattr(settings, action.ttl_setting, action.ttl_default))


def _token_secret(action: PRAction) -> str:
    custom = getattr(settings, action.secret_setting, "").strip()
    if custom:
        return custom
    return settings.SECRET_KEY


def _token_salt(action: PRAction) -> str:
    return getattr(settings, action.salt_setting, action.salt_default)


__all__ = [
    "CLOSE_PR",
    "LABEL_PR",
    "PRAction",
    "PRActionLinkClaims",
    "PRActionTokenError",
    "PRActionTokenExpired",
    "PRActionTokenInvalid",
    "build_pr_action_link",
    "issue_pr_action_token",
    "validate_pr_action_token",
    "ttl_seconds",
]
