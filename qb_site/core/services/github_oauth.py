from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import requests
from django.conf import settings


@dataclass(frozen=True)
class GitHubUserIdentity:
    github_user_id: int
    github_node_id: str
    github_login: str
    github_name: str | None = None
    github_avatar_url: str | None = None


@dataclass(frozen=True)
class GitHubOAuthError(RuntimeError):
    message: str
    payload: dict[str, Any] | None = None

    def __str__(self) -> str:
        if not self.payload:
            return self.message
        return f"{self.message} (payload={self.payload})"


class GitHubOAuthClient:
    def __init__(self, *, timeout: int = 15) -> None:
        self.client_id = getattr(settings, "GITHUB_OAUTH_CLIENT_ID", "").strip()
        self.client_secret = getattr(settings, "GITHUB_OAUTH_CLIENT_SECRET", "").strip()
        self.authorize_url = getattr(settings, "GITHUB_OAUTH_AUTHORIZE_URL", "").strip()
        self.token_url = getattr(settings, "GITHUB_OAUTH_TOKEN_URL", "").strip()
        self.api_url = getattr(settings, "GITHUB_API_URL", "").strip().rstrip("/")
        self.scope = getattr(settings, "GITHUB_OAUTH_SCOPE", "read:user").strip() or "read:user"
        self.timeout = timeout
        if not self.client_id or not self.client_secret:
            raise GitHubOAuthError("GitHub OAuth credentials are not configured")
        if not self.authorize_url or not self.token_url or not self.api_url:
            raise GitHubOAuthError("GitHub OAuth endpoints are not configured")

    def build_authorize_url(self, *, state: str, redirect_uri: str) -> str:
        if not redirect_uri:
            raise GitHubOAuthError("GitHub OAuth redirect URI is not configured")
        query = urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": redirect_uri,
                "scope": self.scope,
                "state": state,
            }
        )
        return f"{self.authorize_url}?{query}"

    def exchange_code_for_access_token(self, *, code: str, redirect_uri: str) -> str:
        if not code:
            raise GitHubOAuthError("Missing GitHub OAuth code")
        if not redirect_uri:
            raise GitHubOAuthError("GitHub OAuth redirect URI is not configured")
        response = requests.post(
            self.token_url,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
            headers={
                "Accept": "application/json",
                "User-Agent": "queueboard-zulip-bot",
            },
            timeout=self.timeout,
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise GitHubOAuthError("GitHub OAuth token exchange failed", payload=_safe_json(response)) from exc
        payload = _safe_json(response)
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise GitHubOAuthError("GitHub OAuth token payload missing access_token", payload=payload)
        return token

    def fetch_user_identity(self, *, access_token: str) -> GitHubUserIdentity:
        if not access_token:
            raise GitHubOAuthError("Missing GitHub access token")
        response = requests.get(
            f"{self.api_url}/user",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {access_token}",
                "User-Agent": "queueboard-zulip-bot",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=self.timeout,
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise GitHubOAuthError("GitHub user fetch failed", payload=_safe_json(response)) from exc
        payload = _safe_json(response)
        github_user_id = payload.get("id")
        github_node_id = payload.get("node_id")
        github_login = payload.get("login")
        if not isinstance(github_user_id, int) or github_user_id <= 0:
            raise GitHubOAuthError("GitHub user payload missing id", payload=payload)
        if not isinstance(github_node_id, str) or not github_node_id:
            raise GitHubOAuthError("GitHub user payload missing node_id", payload=payload)
        if not isinstance(github_login, str) or not github_login:
            raise GitHubOAuthError("GitHub user payload missing login", payload=payload)
        github_name = payload.get("name")
        if github_name is not None and not isinstance(github_name, str):
            github_name = None
        github_avatar_url = payload.get("avatar_url")
        if github_avatar_url is not None and not isinstance(github_avatar_url, str):
            github_avatar_url = None
        return GitHubUserIdentity(
            github_user_id=github_user_id,
            github_node_id=github_node_id,
            github_login=github_login,
            github_name=github_name,
            github_avatar_url=github_avatar_url,
        )


def _safe_json(response: requests.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {"raw": response.text}
    if isinstance(payload, dict):
        return payload
    return {"raw": payload}
