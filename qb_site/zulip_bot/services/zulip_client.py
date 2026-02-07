from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable

import requests
from django.conf import settings


@dataclass(frozen=True)
class ZulipApiError(RuntimeError):
    message: str
    payload: dict[str, Any] | None = None

    def __str__(self) -> str:
        if not self.payload:
            return self.message
        return f"{self.message} (payload={self.payload})"


class ZulipClient:
    """Minimal REST client for Zulip API v1."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        email: str | None = None,
        api_key: str | None = None,
        user_email: str | None = None,
        user_api_key: str | None = None,
        timeout: int = 15,
    ) -> None:
        self.base_url = (base_url or getattr(settings, "ZULIP_BASE_URL", None) or "").rstrip("/")
        self.email = email or getattr(settings, "ZULIP_BOT_EMAIL", None)
        self.api_key = api_key or getattr(settings, "ZULIP_BOT_API_KEY", None)
        self.user_email = user_email or getattr(settings, "ZULIP_USER_EMAIL", None)
        self.user_api_key = user_api_key or getattr(settings, "ZULIP_USER_API_KEY", None)
        self.timeout = timeout
        if not self.base_url:
            raise ZulipApiError("Zulip base URL is not configured")
        if not self.email or not self.api_key:
            raise ZulipApiError("Zulip bot credentials are not configured")

    def send_stream_message(self, *, stream: str | int, topic: str, content: str) -> dict[str, Any]:
        data = {
            "type": "stream",
            "to": stream,
            "topic": topic,
            "content": content,
        }
        return self._request("POST", "/messages", data=data)

    def send_direct_message(self, *, to: Iterable[str | int], content: str) -> dict[str, Any]:
        data = {
            "type": "direct",
            "to": list(to),
            "content": content,
        }
        return self._request("POST", "/messages", data=data)

    def get_user_by_email(self, email: str) -> dict[str, Any]:
        return self._request("GET", f"/users/{email}")

    def get_user_by_id(self, user_id: int) -> dict[str, Any]:
        return self._request("GET", f"/users/{user_id}")

    def get_user_groups(self, *, include_deactivated: bool = False) -> dict[str, Any]:
        """Fetch all user groups (bots/guests are not allowed by Zulip)."""
        params = {"include_deactivated_groups": json.dumps(include_deactivated)}
        return self._request("GET", "/user_groups", params=params)

    def is_user_group_member(self, *, user_group_id: int, user_id: int, direct_member_only: bool = False) -> dict[str, Any]:
        # Zulip expects JSON-encoded booleans in query params ("true"/"false"),
        # not Python bool stringification ("True"/"False").
        params = {"direct_member_only": json.dumps(direct_member_only)}
        return self._request("GET", f"/user_groups/{user_group_id}/members/{user_id}", params=params, auth_mode="user_required")

    def get_user_group_members(self, *, user_group_id: int) -> dict[str, Any]:
        return self._request("GET", f"/user_groups/{user_group_id}/members")

    def update_user_group_members(self, *, user_group_id: int, add: list[int], delete: list[int]) -> dict[str, Any]:
        data = {"add": add, "delete": delete}
        return self._request("POST", f"/user_groups/{user_group_id}/members", data=data)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        auth_mode: str = "bot",
    ) -> dict[str, Any]:
        url = f"{self.base_url}/api/v1{path}"
        auth = self._resolve_auth(auth_mode)
        headers = {"User-Agent": "queueboard-zulip-bot"}
        response = requests.request(
            method,
            url,
            params=params,
            data=data,
            headers=headers,
            auth=auth,
            timeout=self.timeout,
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            payload = self._safe_json(response)
            raise ZulipApiError("Zulip API request failed", payload=payload) from exc

        payload = self._safe_json(response)
        if payload.get("result") != "success":
            raise ZulipApiError("Zulip API returned error", payload=payload)
        return payload

    def _resolve_auth(self, auth_mode: str) -> tuple[str, str]:
        if auth_mode == "bot":
            return (self.email, self.api_key)
        if auth_mode == "user_required":
            if self.user_email and self.user_api_key:
                return (self.user_email, self.user_api_key)
            raise ZulipApiError(
                "Zulip user credentials are required for this endpoint",
                payload={"auth_mode": auth_mode},
            )
        raise ZulipApiError("Unknown Zulip auth mode", payload={"auth_mode": auth_mode})

    def _safe_json(self, response: requests.Response) -> dict[str, Any]:
        try:
            return response.json()
        except json.JSONDecodeError:
            return {"raw": response.text}
