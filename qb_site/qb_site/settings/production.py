"""Production settings."""

from __future__ import annotations

import os

from .base import *  # noqa

DEBUG = False

if SECRET_KEY == "django-insecure-change-me":
    raise RuntimeError("DJANGO_SECRET_KEY must be set for production deployments")

if not ALLOWED_HOSTS:
    raise RuntimeError("DJANGO_ALLOWED_HOSTS must be configured for production")

# WhiteNoise goes directly after SecurityMiddleware, as its docs ask.
_AFTER_SECURITY = MIDDLEWARE.index("django.middleware.security.SecurityMiddleware") + 1
MIDDLEWARE = MIDDLEWARE[:_AFTER_SECURITY] + ["whitenoise.middleware.WhiteNoiseMiddleware"] + MIDDLEWARE[_AFTER_SECURITY:]
# collectstatic writes hashed and compressed copies beside each file, and {% static %} names the
# hashed one, which WhiteNoise serves with a far-future cache. The plain names are still served.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
# Blank, as .env.example ships it, means unset: 0, so no Strict-Transport-Security header.
SECURE_HSTS_SECONDS = int(os.getenv("DJANGO_SECURE_HSTS_SECONDS", "").strip() or "0")
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool(os.getenv("DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS"), False)
SECURE_HSTS_PRELOAD = env_bool(os.getenv("DJANGO_SECURE_HSTS_PRELOAD"), False)
