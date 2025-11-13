"""Base settings shared across all environments."""

from __future__ import annotations

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
APPS_DIR = Path(__file__).resolve().parent.parent

SRC_DIR = BASE_DIR / "src"
if SRC_DIR.exists() and str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def env_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.lower() in {"1", "true", "t", "yes", "y"}


SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "django-insecure-change-me")
DEBUG = env_bool(os.getenv("DJANGO_DEBUG"), False)

ALLOWED_HOSTS = [host.strip() for host in os.getenv("DJANGO_ALLOWED_HOSTS", "").split(",") if host.strip()]

CSRF_TRUSTED_ORIGINS = [origin.strip() for origin in os.getenv("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if origin.strip()]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_celery_results",
    "core",
    "syncer",
    "analyzer",
    "api",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "qb_site.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [str(BASE_DIR / "templates")],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]

WSGI_APPLICATION = "qb_site.wsgi.application"
ASGI_APPLICATION = "qb_site.asgi.application"

DEFAULT_DB_ENGINE = os.getenv("DJANGO_DB_ENGINE", "django.db.backends.postgresql")

DATABASES = {
    "default": {
        "ENGINE": DEFAULT_DB_ENGINE,
        "NAME": os.getenv("DJANGO_DB_NAME", "queueboard"),
        "USER": os.getenv("DJANGO_DB_USER", "queueboard"),
        "PASSWORD": os.getenv("DJANGO_DB_PASSWORD", ""),
        "HOST": os.getenv("DJANGO_DB_HOST", "localhost"),
        "PORT": os.getenv("DJANGO_DB_PORT", "5432"),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.getenv("DJANGO_TIME_ZONE", "UTC")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = os.getenv("DJANGO_STATIC_ROOT", str(BASE_DIR / "staticfiles"))
STATICFILES_DIRS = [path for path in [BASE_DIR / "static"] if path.exists()]

MEDIA_URL = "media/"
MEDIA_ROOT = os.getenv("DJANGO_MEDIA_ROOT", str(BASE_DIR / "media"))

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
        }
    },
    "root": {
        "handlers": ["console"],
        "level": os.getenv("DJANGO_LOG_LEVEL", "INFO"),
    },
}


# Celery configuration
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0")
# Use django-celery-results when explicitly configured via env; fallback to broker
# so existing environments continue to work until .env is updated.
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", CELERY_BROKER_URL)
CELERY_RESULT_EXTENDED = env_bool(os.getenv("CELERY_RESULT_EXTENDED"), True)
CELERY_TASK_TRACK_STARTED = env_bool(os.getenv("CELERY_TASK_TRACK_STARTED"), True)
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_DEFAULT_QUEUE = "default"
CELERY_TASK_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]

# Syncer scheduling defaults (env-overridable)
SYNCER_DISCOVERY_LOOKBACK_MINUTES = int(os.getenv("SYNCER_DISCOVERY_LOOKBACK_MINUTES", 60))
SYNCER_DISCOVERY_LIMIT = int(os.getenv("SYNCER_DISCOVERY_LIMIT", 30))
SYNCER_DISCOVERY_STATES_DEFAULT = [
    s.strip().upper() for s in os.getenv("SYNCER_DISCOVERY_STATES_DEFAULT", "OPEN").split(",") if s.strip()
]
# Rate and paging defaults
SYNCER_RATE_REMAINING_MIN = int(os.getenv("SYNCER_RATE_REMAINING_MIN", 200))
SYNCER_TIMELINE_K_DEFAULT = int(os.getenv("SYNCER_TIMELINE_K_DEFAULT", 150))
SYNCER_COMMITS_M_DEFAULT = int(os.getenv("SYNCER_COMMITS_M_DEFAULT", 15))
SYNCER_ACTIVE_REPOS_PERIOD_SECONDS = int(os.getenv("SYNCER_ACTIVE_REPOS_PERIOD_SECONDS", 300))
SYNCER_REPO_ENQUEUE_BATCH_MAX = int(os.getenv("SYNCER_REPO_ENQUEUE_BATCH_MAX", 30))
SYNCER_EST_COST_PER_PR = int(os.getenv("SYNCER_EST_COST_PER_PR", 150))
SYNCER_TIMELINE_BACKFILL_PAGES = int(os.getenv("SYNCER_TIMELINE_BACKFILL_PAGES", 0))
# Commit backfill per up-to-date run (pages of the commits connection to walk backward)
SYNCER_COMMITS_BACKFILL_PAGES = int(os.getenv("SYNCER_COMMITS_BACKFILL_PAGES", 0))

# Beat schedule: periodically enqueue repo syncs for active repositories.
CELERY_BEAT_SCHEDULE = {
    "sync_active_repos": {
        "task": "syncer.sync_active_repos",
        "schedule": SYNCER_ACTIVE_REPOS_PERIOD_SECONDS,
    },
    "collect_syncer_metrics": {
        "task": "syncer.collect_metrics",
        "schedule": 900,  # 15 minutes
    },
}
