from __future__ import annotations

import logging
import os
from collections.abc import Sequence

from django.conf import settings

from core.services.github_app_tokens import GitHubAppTokenError, get_default_github_app_token_provider

log = logging.getLogger(__name__)


def resolve_github_operation_token(
    *,
    operation: str | None,
    owner: str,
    repo: str,
    setting_token_names: Sequence[str] = (),
) -> str:
    if operation:
        try:
            app_token = get_default_github_app_token_provider().get_token(operation=operation, owner=owner, repo=repo)
        except GitHubAppTokenError as exc:
            log.warning(
                "github_app_token_resolution_failed",
                extra={"code": exc.code, "operation": operation, "owner": owner, "repo": repo},
            )
        else:
            if app_token:
                return app_token

    for setting_name in setting_token_names:
        token = str(getattr(settings, setting_name, "")).strip()
        if token:
            return token

    env_token = os.getenv("GITHUB_ASSIGNMENT_TOKEN", "").strip()
    if env_token:
        return env_token

    env_tokens = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN") or ""
    return next((token.strip() for token in env_tokens.split(",") if token.strip()), "")
