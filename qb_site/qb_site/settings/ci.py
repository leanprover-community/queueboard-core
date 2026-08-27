"""Continuous Integration settings.

Extends base settings and overrides a few values to make tests deterministic
and independent of local .env/.env.example knobs.
"""

from __future__ import annotations

import os

from .base import *  # noqa: F401,F403

# Deterministic defaults for CI
DEBUG = False
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "django-insecure-ci")
ALLOWED_HOSTS = ["*"]

# Use the default database configuration (PostgreSQL via env) for CI.

# Give the suite its own Redis database index, so a test run cannot write into the keyspace a
# developer's `docker compose up` stack is using. Paired with the runner below, which clears the
# app's namespaces before the suite starts — that is what breaks the run-to-run leak, since the
# test database restarts ids from 1 every run and would otherwise collide with the previous run's
# `(repo_id, pr_number)` dedupe keys. Not a deployment knob; the env var is an escape hatch for a
# Redis configured with fewer than 16 databases.
CI_REDIS_DB_INDEX = int(os.getenv("CI_REDIS_DB_INDEX", "15"))


def _with_redis_db_index(url: str, index: int) -> str:
    """Repoint a redis:// or rediss:// URL at another database index, leaving anything else alone."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(str(url or ""))
    if parts.scheme not in ("redis", "rediss"):
        return url
    return urlunsplit((parts.scheme, parts.netloc, f"/{int(index)}", parts.query, parts.fragment))


CELERY_BROKER_URL = _with_redis_db_index(CELERY_BROKER_URL, CI_REDIS_DB_INDEX)  # noqa: F405
CELERY_RESULT_BACKEND = _with_redis_db_index(CELERY_RESULT_BACKEND, CI_REDIS_DB_INDEX)  # noqa: F405

# Ensure CI filtering is disabled unless a test overrides it explicitly
SYNCER_CI_FILTER_MODE = "all"
SYNCER_CI_ALLOW_CHECKRUN_NAMES = ""
SYNCER_CI_ALLOW_STATUS_NAMES = ""
