"""The console's token-less GitHub-OAuth ``state`` (design doc 050).

The Fernet primitive this is built on lives in ``core.services.signed_payloads``; the Zulip
registration flow's state helper uses the same primitive, so all consumers share one implementation.

The ``state`` round-trips CSRF protection: the caller stores a random ``nonce`` in the session,
embeds it here, and on callback confirms the returned state's nonce matches the session — so a
forged callback cannot complete the flow.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings

from core.services.signed_payloads import (
    SignedPayloadInvalid,
    issue_signed_payload,
    read_signed_payload,
)

CONSOLE_OAUTH_STATE_SALT = "core.console.oauth_state"


@dataclass(frozen=True)
class ConsoleOAuthStateClaims:
    nonce: str
    next: str = ""


def _console_secret() -> str:
    return settings.SECRET_KEY


def _console_ttl_seconds() -> int:
    return int(getattr(settings, "CONSOLE_OAUTH_STATE_TTL_SECONDS", 600))


def issue_console_oauth_state(*, claims: ConsoleOAuthStateClaims, now: int | None = None) -> str:
    return issue_signed_payload(
        {"nonce": claims.nonce, "next": claims.next},
        secret=_console_secret(),
        salt=CONSOLE_OAUTH_STATE_SALT,
        ttl_seconds=_console_ttl_seconds(),
        now=now,
    )


def validate_console_oauth_state(state: str, *, now: int | None = None) -> ConsoleOAuthStateClaims:
    payload = read_signed_payload(state, secret=_console_secret(), salt=CONSOLE_OAUTH_STATE_SALT, now=now)
    nonce = payload.get("nonce")
    if not isinstance(nonce, str) or not nonce:
        raise SignedPayloadInvalid("invalid nonce")
    next_path = payload.get("next")
    if next_path is not None and not isinstance(next_path, str):
        raise SignedPayloadInvalid("invalid next")
    return ConsoleOAuthStateClaims(nonce=nonce, next=next_path or "")


__all__ = [
    "CONSOLE_OAUTH_STATE_SALT",
    "ConsoleOAuthStateClaims",
    "issue_console_oauth_state",
    "validate_console_oauth_state",
]
