from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from typing import Any

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from django.conf import settings
from django.utils import timezone

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GitHubAppTokenError(RuntimeError):
    code: str
    message: str


@dataclass(frozen=True)
class GitHubAppDefinition:
    name: str
    app_id: int
    private_key_pem: str
    operations: frozenset[str]


@dataclass(frozen=True)
class _CachedInstallationToken:
    token: str
    expires_at: datetime


class GitHubAppInstallationTokenProvider:
    def __init__(self, *, config: dict[str, Any] | None = None, timeout_seconds: int = 20) -> None:
        raw_config = config if isinstance(config, dict) else {}
        self.api_base_url = str(raw_config.get("api_base_url") or getattr(settings, "GITHUB_API_URL", "")).strip()
        self.api_base_url = (self.api_base_url or "https://api.github.com").rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.cache_skew_seconds = int(raw_config.get("cache_skew_seconds", 60))
        self.operation_app_map = {
            str(operation): str(app_name)
            for operation, app_name in (raw_config.get("operation_app_map") or {}).items()
            if str(operation).strip() and str(app_name).strip()
        }
        self._apps = self._parse_apps(raw_config.get("apps") or [])
        self._apps_by_name = {app.name: app for app in self._apps}
        self._installation_id_cache: dict[tuple[str, str, str], int] = {}
        self._token_cache: dict[tuple[str, int], _CachedInstallationToken] = {}

    def get_token(self, *, operation: str, owner: str, repo: str) -> str | None:
        app = self._select_app(operation=operation)
        if app is None:
            return None

        installation_id = self._resolve_installation_id(app=app, owner=owner, repo=repo)
        cached = self._token_cache.get((app.name, installation_id))
        now = timezone.now()
        if cached is not None and cached.expires_at > now + timedelta(seconds=self.cache_skew_seconds):
            log.info(
                "github_app_token_cache_hit",
                extra={"app_name": app.name, "operation": operation, "owner": owner, "repo": repo},
            )
            return cached.token

        token, expires_at = self._mint_installation_token(app=app, installation_id=installation_id)
        self._token_cache[(app.name, installation_id)] = _CachedInstallationToken(token=token, expires_at=expires_at)
        log.info(
            "github_app_token_minted",
            extra={
                "app_name": app.name,
                "operation": operation,
                "owner": owner,
                "repo": repo,
                "installation_id": installation_id,
                "expires_at": expires_at.isoformat(),
            },
        )
        return token

    def _parse_apps(self, raw_apps: list[Any]) -> list[GitHubAppDefinition]:
        apps: list[GitHubAppDefinition] = []
        for raw in raw_apps:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name", "")).strip()
            app_id_raw = raw.get("app_id")
            if not name or app_id_raw is None:
                continue
            try:
                app_id = int(app_id_raw)
            except (TypeError, ValueError):
                continue
            private_key_pem = self._load_private_key_pem(raw)
            if not private_key_pem:
                continue
            operations = frozenset(str(op).strip() for op in (raw.get("operations") or []) if str(op).strip())
            apps.append(
                GitHubAppDefinition(
                    name=name,
                    app_id=app_id,
                    private_key_pem=private_key_pem,
                    operations=operations,
                )
            )
        return apps

    def _load_private_key_pem(self, raw_app: dict[str, Any]) -> str:
        inline_key = str(raw_app.get("private_key") or "").strip()
        if inline_key:
            return inline_key.replace("\\n", "\n")
        private_key_path = str(raw_app.get("private_key_path") or "").strip()
        if not private_key_path:
            return ""
        try:
            return Path(private_key_path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            log.warning(
                "github_app_private_key_load_failed path=%s error=%s",
                private_key_path,
                exc,
                extra={"private_key_path": private_key_path},
            )
            return ""

    def _select_app(self, *, operation: str) -> GitHubAppDefinition | None:
        mapped_name = self.operation_app_map.get(operation)
        if mapped_name:
            mapped = self._apps_by_name.get(mapped_name)
            if mapped is None:
                raise GitHubAppTokenError(
                    code="invalid_operation_mapping",
                    message=f"GitHub app mapping for `{operation}` references missing app `{mapped_name}`.",
                )
            return mapped
        for app in self._apps:
            if operation in app.operations:
                return app
        return None

    def _resolve_installation_id(self, *, app: GitHubAppDefinition, owner: str, repo: str) -> int:
        cache_key = (app.name, owner.lower(), repo.lower())
        cached = self._installation_id_cache.get(cache_key)
        if cached is not None:
            return cached

        app_jwt = _build_github_app_jwt(app_id=app.app_id, private_key_pem=app.private_key_pem)
        url = f"{self.api_base_url}/repos/{owner}/{repo}/installation"
        response = requests.get(
            url,
            headers=_github_headers_for_app_jwt(app_jwt),
            timeout=self.timeout_seconds,
        )
        payload = _safe_json(response)
        if response.status_code >= 400:
            if response.status_code in {401, 403}:
                raise GitHubAppTokenError(
                    code="app_auth_failed",
                    message=f"GitHub app authentication failed for repository `{owner}/{repo}`.",
                )
            if response.status_code == 404:
                raise GitHubAppTokenError(
                    code="installation_not_found",
                    message=f"GitHub app is not installed for `{owner}/{repo}`.",
                )
            raise GitHubAppTokenError(
                code="installation_lookup_failed",
                message=f"GitHub installation lookup failed for `{owner}/{repo}`: {payload}",
            )

        installation_id = payload.get("id")
        if not isinstance(installation_id, int):
            raise GitHubAppTokenError(
                code="installation_lookup_failed",
                message=f"GitHub installation lookup did not return an installation id for `{owner}/{repo}`.",
            )
        self._installation_id_cache[cache_key] = installation_id
        return installation_id

    def _mint_installation_token(self, *, app: GitHubAppDefinition, installation_id: int) -> tuple[str, datetime]:
        app_jwt = _build_github_app_jwt(app_id=app.app_id, private_key_pem=app.private_key_pem)
        url = f"{self.api_base_url}/app/installations/{installation_id}/access_tokens"
        response = requests.post(
            url,
            headers=_github_headers_for_app_jwt(app_jwt),
            timeout=self.timeout_seconds,
        )
        payload = _safe_json(response)
        if response.status_code >= 400:
            if response.status_code in {401, 403}:
                raise GitHubAppTokenError(
                    code="token_mint_permission_denied",
                    message=f"GitHub denied installation token minting for app `{app.name}`.",
                )
            raise GitHubAppTokenError(
                code="token_mint_failed",
                message=f"GitHub installation token minting failed for app `{app.name}`: {payload}",
            )
        token = payload.get("token")
        expires_at_raw = payload.get("expires_at")
        if not isinstance(token, str) or not token.strip():
            raise GitHubAppTokenError(
                code="token_mint_failed",
                message=f"GitHub token minting response for app `{app.name}` had no token.",
            )
        expires_at = _parse_github_timestamp(expires_at_raw)
        if expires_at is None:
            raise GitHubAppTokenError(
                code="token_mint_failed",
                message=f"GitHub token minting response for app `{app.name}` had invalid expires_at.",
            )
        return token, expires_at


def _github_headers_for_app_jwt(app_jwt: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {app_jwt}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _safe_json(response: requests.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {"raw": response.text}
    if isinstance(payload, dict):
        return payload
    return {"raw": payload}


def _parse_github_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parsed: datetime
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed.astimezone(dt_timezone.utc)


def _build_github_app_jwt(*, app_id: int, private_key_pem: str) -> str:
    if not private_key_pem.strip():
        raise GitHubAppTokenError(code="invalid_private_key", message="GitHub app private key is not configured.")
    now = int(timezone.now().timestamp())
    header_json = json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_json = json.dumps({"iat": now - 60, "exp": now + 540, "iss": str(app_id)}, separators=(",", ":")).encode("utf-8")
    encoded_header = _b64url(header_json)
    encoded_payload = _b64url(payload_json)
    signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
    try:
        private_key = serialization.load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
    except (TypeError, ValueError) as exc:
        raise GitHubAppTokenError(code="invalid_private_key", message="GitHub app private key could not be parsed.") from exc
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{encoded_header}.{encoded_payload}.{_b64url(signature)}"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


_default_provider: GitHubAppInstallationTokenProvider | None = None
_default_provider_signature: str | None = None


def get_default_github_app_token_provider() -> GitHubAppInstallationTokenProvider:
    global _default_provider
    global _default_provider_signature
    raw_config = getattr(settings, "GITHUB_APP_TOKEN_CONFIG", {}) or {}
    signature = json.dumps(raw_config, sort_keys=True, separators=(",", ":"), default=str)
    if _default_provider is None or _default_provider_signature != signature:
        _default_provider = GitHubAppInstallationTokenProvider(config=raw_config)
        _default_provider_signature = signature
    return _default_provider
