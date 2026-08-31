"""Signed, expiring payloads: the one Fernet primitive behind every opaque string we hand out.

Encryption + integrity + a TTL over a JSON dict. Four kinds of consumer share it:

- GitHub-OAuth ``state`` (``core.services.oauth_state``, ``zulip_bot.services.registration_oauth_state``)
- the registration link (``zulip_bot.services.registration_links``)
- the per-PR action links (``zulip_bot.services.pr_action_links``)

It lives here rather than in ``oauth_state`` — where it started — because only half its consumers are
OAuth states. Each consumer supplies its own secret and salt, so payloads of one kind never validate
as another; and each owns its own claims dataclass and exception types, because "is this payload
well-formed" is a per-consumer question. This module answers only "is it ours, intact, and unexpired".

``now`` is injectable on both calls so tests can pin a clock without patching ``time`` in whichever
module happens to read it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


class SignedPayloadError(Exception):
    """Base class for signed-payload failures."""


class SignedPayloadExpired(SignedPayloadError):
    """The payload's ``exp`` is in the past."""


class SignedPayloadInvalid(SignedPayloadError):
    """The payload is malformed, tampered with, or undecryptable."""


def _fernet(*, secret: str, salt: str) -> Fernet:
    material = f"{secret}:{salt}".encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(material).digest())
    return Fernet(key)


def issue_signed_payload(
    payload: dict[str, Any],
    *,
    secret: str,
    salt: str,
    ttl_seconds: int,
    now: int | None = None,
) -> str:
    """Encrypt ``payload`` (plus ``iat``/``exp``) into an opaque, URL-safe string."""
    now_ts = int(now if now is not None else time.time())
    body = dict(payload)
    body["iat"] = now_ts
    body["exp"] = now_ts + int(ttl_seconds)
    encrypted = _fernet(secret=secret, salt=salt).encrypt(json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return encrypted.decode("utf-8")


def read_signed_payload(value: str, *, secret: str, salt: str, now: int | None = None) -> dict[str, Any]:
    """Decrypt + verify a payload string. Raises ``SignedPayloadInvalid``/``SignedPayloadExpired``.

    Expiry is checked here, before the caller inspects any claim, so an expired payload reports as
    expired even when its body is also malformed. Callers validate their own claims afterwards.
    """
    try:
        decrypted = _fernet(secret=secret, salt=salt).decrypt(value.encode("utf-8"))
    except (InvalidToken, ValueError, UnicodeEncodeError) as exc:
        raise SignedPayloadInvalid("invalid payload") from exc
    try:
        payload = json.loads(decrypted.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SignedPayloadInvalid("invalid payload") from exc
    if not isinstance(payload, dict):
        raise SignedPayloadInvalid("invalid payload")
    exp = payload.get("exp")
    if not isinstance(exp, int):
        raise SignedPayloadInvalid("invalid exp")
    if int(now if now is not None else time.time()) > exp:
        raise SignedPayloadExpired("payload expired")
    return payload


__all__ = [
    "SignedPayloadError",
    "SignedPayloadExpired",
    "SignedPayloadInvalid",
    "issue_signed_payload",
    "read_signed_payload",
]
