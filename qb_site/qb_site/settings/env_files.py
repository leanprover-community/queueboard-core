"""Read allowlisted settings from files named by ``<NAME>_FILE`` environment variables (design doc 058).

Some values do not fit in an env file: JSON objects with inline private keys, multi-line text, and
secrets containing quotes, ``$`` or ``#``, which some env-file formats interpolate or cut short.
For each name in ``FILE_BACKED_ENV_VARS`` a deployment may instead set ``<NAME>_FILE`` to the path
of a file holding the value, e.g. a container secret mounted read-only.

``resolve_env_files()`` runs once, at the top of ``base.py``, before any setting is read. It copies
each file's contents into ``os.environ[<NAME>]`` and removes ``<NAME>_FILE``. Resolving into the
environment, rather than wrapping each read, is what reaches the readers that bypass Django
settings: the ``GH_TOKEN``/``GITHUB_TOKEN`` reads in the syncer services, Celery's own lookup of
``CELERY_BROKER_URL``/``CELERY_RESULT_BACKEND``, and the ``DJANGO_SECRET_KEY`` re-reads in
``local.py``/``ci.py``. Removing ``<NAME>_FILE`` keeps a child process that inherits the
environment (the ``runserver`` autoreloader) from seeing both variables and failing the check below.

Rules:
- ``<NAME>`` and ``<NAME>_FILE`` both set is an error. An empty value counts as unset, for either
  variable, so a template line ``NAME=`` left blank does not conflict with ``NAME_FILE``.
- A file that is missing, unreadable, not UTF-8, or holds a NUL byte is an error. So is a file
  that is empty, or holds only a newline: that is a secret that failed to render, and starting
  with a blank value would only fail later, on first use.
- One trailing newline is stripped, and nothing else: the value is otherwise exactly what the
  variable would have held, so readers that strip (the JSON settings, the token lists) still do.
- Error messages name variables and paths, never values.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping
from pathlib import Path

# Settings that may be read from a file. Secrets, and the JSON settings. Names outside this list
# are ignored: other `*_FILE` variables (SSL_CERT_FILE, ...) belong to other tools.
FILE_BACKED_ENV_VARS: tuple[str, ...] = (
    "DJANGO_SECRET_KEY",
    "DJANGO_DB_PASSWORD",
    # Broker/result URLs can embed a password.
    "CELERY_BROKER_URL",
    "CELERY_RESULT_BACKEND",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GITHUB_OAUTH_CLIENT_SECRET",
    "GITHUB_WEBHOOK_SECRET",
    "GITHUB_APP_TOKEN_CONFIG",
    "ZULIP_WEBHOOK_TOKEN",
    "ZULIP_BOT_API_KEY",
    "ZULIP_USER_API_KEY",
    "ZULIP_LINK_TOKEN_SECRET",
    "ZULIP_PREFS_TOKEN_SECRET",
    "ZULIP_CLOSE_PR_TOKEN_SECRET",
    "ZULIP_LABEL_PR_TOKEN_SECRET",
    "ZULIP_COMMAND_POLICY",
    "ZULIP_REPO_LOG",
    "SITE_ANALYTICS_HASH_SALT",
)


def resolve_env_files(environ: MutableMapping[str, str] | None = None) -> list[str]:
    """Move each allowlisted ``<NAME>_FILE``'s contents into ``<NAME>``; return the names moved."""
    env = os.environ if environ is None else environ
    resolved: list[str] = []
    for name in FILE_BACKED_ENV_VARS:
        file_var = f"{name}_FILE"
        path = env.get(file_var, "")
        if not path:
            continue
        if env.get(name, ""):
            raise RuntimeError(f"{name} and {file_var} are both set; set only one")
        env[name] = _read_env_file(file_var, path)
        del env[file_var]
        resolved.append(name)
    return resolved


def _read_env_file(file_var: str, path: str) -> str:
    """Return the file's contents minus one trailing newline; errors name ``file_var`` and ``path``."""
    value, problem = _read_text(file_var, path)
    # Raised here, not in `_read_text`: no exception is being handled, so the error has no
    # `__context__` (a decode error holds the whole file), and no frame left on the traceback holds
    # the contents.
    if value is None:
        raise RuntimeError(f"{file_var}: {path} {problem}")
    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith("\n"):
        value = value[:-1]
    if not value:
        raise RuntimeError(f"{file_var}: {path} is empty")
    return value


def _read_text(file_var: str, path: str) -> tuple[str | None, str]:
    """Return ``(contents, "")``, or ``(None, problem)`` when the contents can't be a value."""
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise RuntimeError(f"{file_var}: cannot read {path}: {exc.strerror or type(exc).__name__}") from exc
    if b"\x00" in raw:
        return None, "contains a NUL byte, which an environment variable cannot hold"
    try:
        return raw.decode("utf-8"), ""
    except UnicodeDecodeError:
        return None, "is not valid UTF-8"
