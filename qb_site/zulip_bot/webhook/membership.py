from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, TypedDict

from zulip_bot.services.zulip_client import ZulipApiError, ZulipClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GroupMembershipCheckError(RuntimeError):
    message: str
    payload: dict[str, Any] | None = None

    def __str__(self) -> str:
        if not self.payload:
            return self.message
        return f"{self.message} (payload={self.payload})"


class ZulipGroupMembershipResponse(TypedDict):
    result: str
    msg: str
    is_user_group_member: bool


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

        errors: list[dict[str, Any]] = []
        for group_id in group_ids:
            try:
                if self._is_member(user_id=user_id, group_id=group_id):
                    return True
            except GroupMembershipCheckError as exc:
                errors.append(exc.payload or {"group_id": group_id, "error": str(exc)})

        if errors:
            raise GroupMembershipCheckError(
                "Zulip group membership check failed",
                payload={
                    "user_id": user_id,
                    "group_ids": sorted(group_ids),
                    "errors": errors,
                },
            )

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
            payload = client.is_user_group_member(user_group_id=group_id, user_id=user_id)
        except ZulipApiError as exc:
            logger.exception("zulip_group_membership_check_failed", extra={"user_id": user_id, "group_id": group_id})
            raise GroupMembershipCheckError(
                "Zulip group membership check failed",
                payload={
                    "user_id": user_id,
                    "group_id": group_id,
                    "zulip_error": exc.payload or {"message": str(exc)},
                },
            ) from exc

        is_member = _parse_is_user_group_member(payload)
        if is_member is not None:
            self._cache[key] = is_member
            return is_member

        raise GroupMembershipCheckError(
            "Zulip group membership payload was missing 'is_user_group_member'",
            payload={
                "user_id": user_id,
                "group_id": group_id,
                "zulip_response": payload,
            },
        )

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


def _parse_is_user_group_member(payload: dict[str, Any]) -> bool | None:
    parsed: ZulipGroupMembershipResponse | None = None
    if isinstance(payload.get("is_user_group_member"), bool):
        parsed = {
            "result": str(payload.get("result", "")),
            "msg": str(payload.get("msg", "")),
            "is_user_group_member": payload["is_user_group_member"],
        }
    if parsed is None:
        return None
    return parsed["is_user_group_member"]
