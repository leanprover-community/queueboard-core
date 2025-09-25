"""Local development settings."""

from __future__ import annotations

import os

from .base import *  # noqa: F401,F403 - import base defaults

DEBUG = True
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "django-insecure-local")
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]"]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
