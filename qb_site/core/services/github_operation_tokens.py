from __future__ import annotations

import logging

from core.services.github_app_tokens import GitHubAppTokenError, get_default_github_app_token_provider

log = logging.getLogger(__name__)


def resolve_github_app_operation_token(
    *,
    operation: str | None,
    owner: str,
    repo: str,
) -> str | None:
    if operation:
        try:
            app_token = get_default_github_app_token_provider().get_token(operation=operation, owner=owner, repo=repo)
        except GitHubAppTokenError as exc:
            log.warning(
                "github_app_token_resolution_failed code=%s operation=%s repo=%s/%s message=%s",
                exc.code,
                operation,
                owner,
                repo,
                exc.message,
                extra={"code": exc.code, "operation": operation, "owner": owner, "repo": repo},
            )
        else:
            if app_token:
                return app_token
    return None
