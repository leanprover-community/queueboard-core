"""Signed, expiring OAuth ``state`` payloads shared by every GitHub-OAuth flow.

A small Fernet-based primitive (encrypt + integrity + TTL over a JSON dict) plus a token-less
console helper built on it. The Zulip registration flow's state helper delegates to the same
primitive so all consumers share one implementation (design doc 050).

The ``state`` round-trips CSRF protection: the caller stores a random ``nonce`` in the session,
embeds it here, and on callback confirms the returned state's nonce matches the session — so a
forged callback cannot complete the flow.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


class SignedStateError(Exception):
    """Base class for signed-state failures."""


class SignedStateExpired(SignedStateError):
    """The state's ``exp`` is in the past."""


class SignedStateInvalid(SignedStateError):
    """The state is malformed, tampered with, or undecryptable."""


def _fernet(*, secret: str, salt: str) -> Fernet:
    material = f"{secret}:{salt}".encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(material).digest())
    return Fernet(key)


def issue_signed_state(
    payload: dict[str, Any],
    *,
    secret: str,
    salt: str,
    ttl_seconds: int,
    now: int | None = None,
) -> str:
    """Encrypt ``payload`` (plus ``iat``/``exp``) into an opaque, URL-safe state string."""
    now_ts = int(now if now is not None else time.time())
    body = dict(payload)
    body["iat"] = now_ts
    body["exp"] = now_ts + int(ttl_seconds)
    encrypted = _fernet(secret=secret, salt=salt).encrypt(json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return encrypted.decode("utf-8")


def read_signed_state(state: str, *, secret: str, salt: str, now: int | None = None) -> dict[str, Any]:
    """Decrypt + verify a state string. Raises ``SignedStateInvalid``/``SignedStateExpired``."""
    try:
        decrypted = _fernet(secret=secret, salt=salt).decrypt(state.encode("utf-8"))
    except (InvalidToken, ValueError, UnicodeEncodeError) as exc:
        raise SignedStateInvalid("invalid state") from exc
    try:
        payload = json.loads(decrypted.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SignedStateInvalid("invalid payload") from exc
    if not isinstance(payload, dict):
        raise SignedStateInvalid("invalid payload")
    exp = payload.get("exp")
    if not isinstance(exp, int):
        raise SignedStateInvalid("invalid exp")
    if int(now if now is not None else time.time()) > exp:
        raise SignedStateExpired("state expired")
    return payload


# --- Console (token-less) OAuth state -------------------------------------------------

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
    return issue_signed_state(
        {"nonce": claims.nonce, "next": claims.next},
        secret=_console_secret(),
        salt=CONSOLE_OAUTH_STATE_SALT,
        ttl_seconds=_console_ttl_seconds(),
        now=now,
    )


def validate_console_oauth_state(state: str, *, now: int | None = None) -> ConsoleOAuthStateClaims:
    payload = read_signed_state(state, secret=_console_secret(), salt=CONSOLE_OAUTH_STATE_SALT, now=now)
    nonce = payload.get("nonce")
    if not isinstance(nonce, str) or not nonce:
        raise SignedStateInvalid("invalid nonce")
    next_path = payload.get("next")
    if next_path is not None and not isinstance(next_path, str):
        raise SignedStateInvalid("invalid next")
    return ConsoleOAuthStateClaims(nonce=nonce, next=next_path or "")


__all__ = [
    "SignedStateError",
    "SignedStateExpired",
    "SignedStateInvalid",
    "issue_signed_state",
    "read_signed_state",
    "ConsoleOAuthStateClaims",
    "issue_console_oauth_state",
    "validate_console_oauth_state",
]
