"""Continuous Integration settings."""

from __future__ import annotations

import os

from .base import *  # noqa: F401,F403 - import base defaults

DEBUG = False
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "django-insecure-ci")
ALLOWED_HOSTS = ["*"]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "ci.sqlite3",
    }
}
