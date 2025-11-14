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

# Lightweight SQLite DB for tests
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "ci.sqlite3",
    }
}

# Ensure CI filtering is disabled unless a test overrides it explicitly
SYNCER_CI_FILTER_MODE = "all"
SYNCER_CI_ALLOW_CHECKRUN_NAMES = ""
SYNCER_CI_ALLOW_STATUS_NAMES = ""
