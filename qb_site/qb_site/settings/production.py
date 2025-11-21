"""Production settings."""

from __future__ import annotations

import os

from .base import *  # noqa

DEBUG = False

if SECRET_KEY == "django-insecure-change-me":
    raise RuntimeError("DJANGO_SECRET_KEY must be set for production deployments")

if not ALLOWED_HOSTS:
    raise RuntimeError("DJANGO_ALLOWED_HOSTS must be configured for production")

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_HSTS_SECONDS = int(os.getenv("DJANGO_SECURE_HSTS_SECONDS", "0"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool(os.getenv("DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS"), False)
SECURE_HSTS_PRELOAD = env_bool(os.getenv("DJANGO_SECURE_HSTS_PRELOAD"), False)
