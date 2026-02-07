from __future__ import annotations

import logging

from zulip_bot.services.zulip_client import ZulipApiError, ZulipClient

logger = logging.getLogger(__name__)


class GroupMembershipChecker:
    def __init__(self) -> None:
        self._cache: dict[tuple[int, int], bool] = {}
        self._client: ZulipClient | None = None
        self._client_unavailable = False

    def is_member_any(self, *, user_id: int | None, group_ids: frozenset[int] | None) -> bool:
        if group_ids is None:
            return True
        if not group_ids:
            return False
        if user_id is None:
            return False

        for group_id in group_ids:
            if self._is_member(user_id=user_id, group_id=group_id):
                return True
        return False

    def _is_member(self, *, user_id: int, group_id: int) -> bool:
        key = (user_id, group_id)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        client = self._get_client()
        if client is None:
            self._cache[key] = False
            return False

        try:
            try:
                payload = client.is_user_group_member(user_group_id=group_id, user_id=user_id)
                is_member = payload.get("is_user_in_group")
                if isinstance(is_member, bool):
                    self._cache[key] = is_member
                    return is_member
            except ZulipApiError as exc:
                if not _is_bot_restricted_group_membership_error(exc):
                    raise

            fallback_payload = client.get_user_group_members(user_group_id=group_id)
            members = fallback_payload.get("members", [])
            result = user_id in members
            self._cache[key] = result
            return result
        except ZulipApiError:
            logger.exception("zulip_group_membership_check_failed", extra={"user_id": user_id, "group_id": group_id})
            self._cache[key] = False
            return False

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


def _is_bot_restricted_group_membership_error(exc: ZulipApiError) -> bool:
    payload = exc.payload or {}
    if not isinstance(payload, dict):
        return False
    msg = payload.get("msg")
    if not isinstance(msg, str):
        return False
    return "does not accept bot requests" in msg.lower()
