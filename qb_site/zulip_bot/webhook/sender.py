from __future__ import annotations

import logging
from typing import Any, TypedDict

from zulip_bot.services.zulip_client import ZulipApiError, ZulipClient

logger = logging.getLogger(__name__)


class ZulipUser(TypedDict):
    is_bot: bool


class ZulipGetUserResponse(TypedDict):
    result: str
    msg: str
    user: ZulipUser


class SenderClassifier:
    """Classifies webhook senders, with Zulip API fallback and local cache.

    This wrapper keeps sender lookup concerns isolated so future integrations
    (for example, core.User / ReviewerPreferences mapping) can replace or
    extend the identity source without changing webhook routing.
    """

    def __init__(self) -> None:
        self._client: ZulipClient | None = None
        self._client_unavailable = False
        self._is_bot_cache: dict[int, bool] = {}

    def is_bot_sender(self, payload: dict[str, Any]) -> bool:
        sender_id = self._extract_sender_id(payload)
        if sender_id is None:
            return False

        cached = self._is_bot_cache.get(sender_id)
        if cached is not None:
            return cached

        client = self._get_client()
        if client is None:
            return False

        try:
            response = client.get_user_by_id(sender_id)
        except ZulipApiError:
            logger.exception("zulip_sender_lookup_failed", extra={"sender_id": sender_id})
            return False

        is_bot = self._response_is_bot(response)
        self._is_bot_cache[sender_id] = is_bot
        return is_bot

    def _get_client(self) -> ZulipClient | None:
        if self._client is not None:
            return self._client
        if self._client_unavailable:
            return None
        try:
            self._client = ZulipClient()
            return self._client
        except ZulipApiError:
            logger.exception("zulip_client_not_configured")
            self._client_unavailable = True
            return None

    def _extract_sender_id(self, payload: dict[str, Any]) -> int | None:
        message = payload.get("message")
        if isinstance(message, dict) and isinstance(message.get("sender_id"), int):
            return message.get("sender_id")
        return None

    def _response_is_bot(self, response: dict[str, Any]) -> bool:
        parsed = _parse_get_user_response(response)
        if parsed is None:
            return False
        return parsed["user"]["is_bot"]


def _parse_get_user_response(payload: dict[str, Any]) -> ZulipGetUserResponse | None:
    user = payload.get("user")
    if not isinstance(user, dict):
        return None
    is_bot = user.get("is_bot")
    if not isinstance(is_bot, bool):
        return None
    return {
        "result": str(payload.get("result", "")),
        "msg": str(payload.get("msg", "")),
        "user": {"is_bot": is_bot},
    }
