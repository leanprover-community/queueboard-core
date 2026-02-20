"""Base settings shared across all environments."""

from __future__ import annotations

import json
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
    "rest_framework",
    "core",
    "syncer",
    "analyzer",
    "api",
    "zulip_bot",
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
FORMAT_MODULE_PATH = "qb_site.formats"
DATETIME_FORMAT = "Y-m-d H:i:s"
SHORT_DATETIME_FORMAT = "Y-m-d H:i:s"

STATIC_URL = "/static/"
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

ZULIP_WEBHOOK_TOKEN = os.getenv("ZULIP_WEBHOOK_TOKEN")
ZULIP_BASE_URL = os.getenv("ZULIP_BASE_URL", "")
ZULIP_BOT_EMAIL = os.getenv("ZULIP_BOT_EMAIL", "")
ZULIP_BOT_API_KEY = os.getenv("ZULIP_BOT_API_KEY", "")
ZULIP_USER_EMAIL = os.getenv("ZULIP_USER_EMAIL", "")
ZULIP_USER_API_KEY = os.getenv("ZULIP_USER_API_KEY", "")
ZULIP_PREFS_URL_BASE = os.getenv("ZULIP_PREFS_URL_BASE", "")
ZULIP_PREFS_TOKEN_SECRET = os.getenv("ZULIP_PREFS_TOKEN_SECRET", "")
ZULIP_PREFS_TOKEN_SALT = os.getenv("ZULIP_PREFS_TOKEN_SALT", "zulip_bot.prefs")
ZULIP_PREFS_TOKEN_TTL_SECONDS = int(os.getenv("ZULIP_PREFS_TOKEN_TTL_SECONDS", 1800))
ZULIP_REGISTRATION_TOKEN_SALT = os.getenv("ZULIP_REGISTRATION_TOKEN_SALT", "zulip_bot.registration")
ZULIP_REGISTRATION_TOKEN_TTL_SECONDS = int(os.getenv("ZULIP_REGISTRATION_TOKEN_TTL_SECONDS", 1800))
ZULIP_REGISTRATION_OAUTH_STATE_SALT = os.getenv("ZULIP_REGISTRATION_OAUTH_STATE_SALT", "zulip_bot.registration.oauth_state")
ZULIP_REGISTRATION_OAUTH_STATE_TTL_SECONDS = int(os.getenv("ZULIP_REGISTRATION_OAUTH_STATE_TTL_SECONDS", 600))
ZULIP_ASSIGNMENT_SUCCESS_EMOJI = os.getenv("ZULIP_ASSIGNMENT_SUCCESS_EMOJI", "thumbs_up")
ZULIP_ASSIGNMENT_MUTATIONS_ENABLED = os.getenv("ZULIP_ASSIGNMENT_MUTATIONS_ENABLED", "")
GITHUB_ASSIGNMENT_TOKEN = os.getenv("GITHUB_ASSIGNMENT_TOKEN", "")
GITHUB_OAUTH_CLIENT_ID = os.getenv("GITHUB_OAUTH_CLIENT_ID", "")
GITHUB_OAUTH_CLIENT_SECRET = os.getenv("GITHUB_OAUTH_CLIENT_SECRET", "")
GITHUB_OAUTH_REDIRECT_URI = os.getenv("GITHUB_OAUTH_REDIRECT_URI", "")
GITHUB_OAUTH_AUTHORIZE_URL = os.getenv("GITHUB_OAUTH_AUTHORIZE_URL", "https://github.com/login/oauth/authorize")
GITHUB_OAUTH_TOKEN_URL = os.getenv("GITHUB_OAUTH_TOKEN_URL", "https://github.com/login/oauth/access_token")
GITHUB_API_URL = os.getenv("GITHUB_API_URL", "https://api.github.com")
GITHUB_OAUTH_SCOPE = os.getenv("GITHUB_OAUTH_SCOPE", "read:user")
ZULIP_COMMAND_POLICY: dict[str, dict[str, list[int | str]]] = {}
_ZULIP_COMMAND_POLICY_ENV = os.getenv("ZULIP_COMMAND_POLICY", "").strip()
if _ZULIP_COMMAND_POLICY_ENV:
    parsed_policy = json.loads(_ZULIP_COMMAND_POLICY_ENV)
    if not isinstance(parsed_policy, dict):
        raise RuntimeError("ZULIP_COMMAND_POLICY env var must be a JSON object")
    ZULIP_COMMAND_POLICY = parsed_policy


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
# Optional Celery queue name for GitHub-bound syncer tasks; when set,
# route GitHub work onto this queue so a dedicated worker can throttle it.
SYNCER_GITHUB_QUEUE = os.getenv("SYNCER_GITHUB_QUEUE", "")
if SYNCER_GITHUB_QUEUE:
    CELERY_TASK_ROUTES = {
        "syncer.sync_repo_since": {"queue": SYNCER_GITHUB_QUEUE},
        "syncer.sync_active_repos": {"queue": SYNCER_GITHUB_QUEUE},
        "syncer.sync_pr": {"queue": SYNCER_GITHUB_QUEUE},
        "syncer.sync_ci_for_shas": {"queue": SYNCER_GITHUB_QUEUE},
        "syncer.refresh_pending_ci_for_repo": {"queue": SYNCER_GITHUB_QUEUE},
        "syncer.refresh_pending_ci_for_active_repos": {"queue": SYNCER_GITHUB_QUEUE},
        "syncer.backfill_repo_history": {"queue": SYNCER_GITHUB_QUEUE},
        "syncer.backfill_repo_history_active": {"queue": SYNCER_GITHUB_QUEUE},
        "syncer.backfill_repo_incomplete_prs": {"queue": SYNCER_GITHUB_QUEUE},
        "syncer.backfill_repo_incomplete_prs_active": {"queue": SYNCER_GITHUB_QUEUE},
        "syncer.backfill_repo_engagement": {"queue": SYNCER_GITHUB_QUEUE},
        "syncer.backfill_repo_engagement_active": {"queue": SYNCER_GITHUB_QUEUE},
        "syncer.harvest_commit_history": {"queue": SYNCER_GITHUB_QUEUE},
        "syncer.harvest_commit_history_sweep": {"queue": SYNCER_GITHUB_QUEUE},
    }

# Syncer scheduling defaults (env-overridable)
SYNCER_DISCOVERY_LOOKBACK_MINUTES = int(os.getenv("SYNCER_DISCOVERY_LOOKBACK_MINUTES", 60))
SYNCER_DISCOVERY_LIMIT = int(os.getenv("SYNCER_DISCOVERY_LIMIT", 100))
SYNCER_DISCOVERY_STATES_DEFAULT = [
    s.strip().upper() for s in os.getenv("SYNCER_DISCOVERY_STATES_DEFAULT", "OPEN,MERGED,CLOSED").split(",") if s.strip()
]
# Rate and paging defaults
SYNCER_RATE_REMAINING_MIN = int(os.getenv("SYNCER_RATE_REMAINING_MIN", 200))
SYNCER_TIMELINE_K_DEFAULT = int(os.getenv("SYNCER_TIMELINE_K_DEFAULT", 150))
SYNCER_COMMITS_M_DEFAULT = int(os.getenv("SYNCER_COMMITS_M_DEFAULT", 15))
SYNCER_LAST_SYNC_EPSILON_SECONDS = int(os.getenv("SYNCER_LAST_SYNC_EPSILON_SECONDS", 300))
SYNCER_ACTIVE_REPOS_PERIOD_SECONDS = int(os.getenv("SYNCER_ACTIVE_REPOS_PERIOD_SECONDS", 300))
SYNCER_REPO_ENQUEUE_BATCH_MAX = int(os.getenv("SYNCER_REPO_ENQUEUE_BATCH_MAX", 30))
SYNCER_EST_COST_PER_PR = int(os.getenv("SYNCER_EST_COST_PER_PR", 150))
SYNCER_TIMELINE_BACKFILL_PAGES = int(os.getenv("SYNCER_TIMELINE_BACKFILL_PAGES", 2))
# Commit backfill per up-to-date run (pages of the commits connection to walk backward)
SYNCER_COMMITS_BACKFILL_PAGES = int(os.getenv("SYNCER_COMMITS_BACKFILL_PAGES", 2))
SYNCER_CI_BY_SHA_PAGES = int(os.getenv("SYNCER_CI_BY_SHA_PAGES", 1))
SYNCER_CI_SHA_BACKOFF_EMPTY_SECONDS = int(os.getenv("SYNCER_CI_SHA_BACKOFF_EMPTY_SECONDS", 300))
SYNCER_CI_SHA_BACKOFF_ERROR_SECONDS = int(os.getenv("SYNCER_CI_SHA_BACKOFF_ERROR_SECONDS", 300))
SYNCER_CI_SHA_SETTLE_WINDOW_SECONDS = int(os.getenv("SYNCER_CI_SHA_SETTLE_WINDOW_SECONDS", 1800))
SYNCER_CI_SHA_HARD_CAP_DAYS = int(os.getenv("SYNCER_CI_SHA_HARD_CAP_DAYS", 400))
SYNCER_CI_SHA_MIN_ATTEMPTS_TERMINAL = int(os.getenv("SYNCER_CI_SHA_MIN_ATTEMPTS_TERMINAL", 2))
SYNCER_GH_THROTTLE_MS = int(os.getenv("SYNCER_GH_THROTTLE_MS", 250))
SYNCER_GH_THROTTLE_MAX_WAIT_MS = int(os.getenv("SYNCER_GH_THROTTLE_MAX_WAIT_MS", 5000))

# History backfill defaults (createdAt-based)
SYNCER_HISTORY_BACKFILL_PAGE_SIZE = int(os.getenv("SYNCER_HISTORY_BACKFILL_PAGE_SIZE", 50))
SYNCER_HISTORY_BACKFILL_MAX_PAGES = int(os.getenv("SYNCER_HISTORY_BACKFILL_MAX_PAGES", 1))
SYNCER_HISTORY_BACKFILL_STATES_DEFAULT = [
    s.strip().upper() for s in os.getenv("SYNCER_HISTORY_BACKFILL_STATES_DEFAULT", "OPEN,MERGED,CLOSED").split(",") if s.strip()
]
SYNCER_HISTORY_BACKFILL_PERIOD_SECONDS = int(os.getenv("SYNCER_HISTORY_BACKFILL_PERIOD_SECONDS", 600))

# Incomplete-PR backfill defaults (DB-based)
SYNCER_INCOMPLETE_BACKFILL_PERIOD_SECONDS = int(os.getenv("SYNCER_INCOMPLETE_BACKFILL_PERIOD_SECONDS", 600))
SYNCER_INCOMPLETE_BACKFILL_LIMIT = int(os.getenv("SYNCER_INCOMPLETE_BACKFILL_LIMIT", 20))
# Engagement backfill (one-off snapshot of files/assignees/approvals/comments)
SYNCER_ENGAGEMENT_BACKFILL_PERIOD_SECONDS = int(os.getenv("SYNCER_ENGAGEMENT_BACKFILL_PERIOD_SECONDS", 900))
SYNCER_ENGAGEMENT_BACKFILL_LIMIT = int(os.getenv("SYNCER_ENGAGEMENT_BACKFILL_LIMIT", 5))

# Pending-CI refresh defaults
SYNCER_PENDING_CI_MAX_AGE_HOURS = int(os.getenv("SYNCER_PENDING_CI_MAX_AGE_HOURS", 48))
SYNCER_PENDING_CI_REFRESH_PERIOD_SECONDS = int(os.getenv("SYNCER_PENDING_CI_REFRESH_PERIOD_SECONDS", 600))
SYNCER_PENDING_CI_REFRESH_MAX_PRS = int(os.getenv("SYNCER_PENDING_CI_REFRESH_MAX_PRS", 5))
SYNCER_PENDING_CI_REFRESH_MAX_SHAS_PER_PR = int(os.getenv("SYNCER_PENDING_CI_REFRESH_MAX_SHAS_PER_PR", 5))

# Commit-history harvest sweep defaults
SYNCER_COMMIT_HISTORY_SWEEP_PERIOD_SECONDS = int(os.getenv("SYNCER_COMMIT_HISTORY_SWEEP_PERIOD_SECONDS", 600))
SYNCER_COMMIT_HISTORY_SWEEP_MAX_JOBS = int(os.getenv("SYNCER_COMMIT_HISTORY_SWEEP_MAX_JOBS", 25))
SYNCER_COMMIT_HISTORY_SWEEP_MAX_PAGES = int(os.getenv("SYNCER_COMMIT_HISTORY_SWEEP_MAX_PAGES", 1))
SYNCER_COMMIT_HISTORY_SWEEP_PAGE_SIZE = int(os.getenv("SYNCER_COMMIT_HISTORY_SWEEP_PAGE_SIZE", 20))

# Analyzer missing-CI sweep defaults
ANALYZER_MISSING_CI_SWEEP_PERIOD_SECONDS = int(os.getenv("ANALYZER_MISSING_CI_SWEEP_PERIOD_SECONDS", 600))
ANALYZER_MISSING_CI_SWEEP_MAX_PRS_PER_REPO = int(os.getenv("ANALYZER_MISSING_CI_SWEEP_MAX_PRS_PER_REPO", 20))
ANALYZER_MISSING_CI_SWEEP_SHAS_PER_PR = int(os.getenv("ANALYZER_MISSING_CI_SWEEP_SHAS_PER_PR", 2))
ANALYZER_MISSING_CI_SWEEP_ONLY_COMPLETE_BACKFILL = env_bool(os.getenv("ANALYZER_MISSING_CI_SWEEP_ONLY_COMPLETE_BACKFILL"), False)
ANALYZER_REVISION_SWEEP_PERIOD_SECONDS = int(os.getenv("ANALYZER_REVISION_SWEEP_PERIOD_SECONDS", 600))
ANALYZER_REVISION_SWEEP_MAX_PRS_PER_REPO = int(os.getenv("ANALYZER_REVISION_SWEEP_MAX_PRS_PER_REPO", 100))
ANALYZER_REVISION_SWEEP_ONLY_COMPLETE_BACKFILL = env_bool(os.getenv("ANALYZER_REVISION_SWEEP_ONLY_COMPLETE_BACKFILL"), False)
ANALYZER_QUEUE_WINDOWS_SWEEP_PERIOD_SECONDS = int(os.getenv("ANALYZER_QUEUE_WINDOWS_SWEEP_PERIOD_SECONDS", 600))
ANALYZER_QUEUE_WINDOWS_SWEEP_MAX_PRS_PER_REPO = int(os.getenv("ANALYZER_QUEUE_WINDOWS_SWEEP_MAX_PRS_PER_REPO", 100))
ANALYZER_QUEUE_WINDOWS_SWEEP_ONLY_COMPLETE_BACKFILL = env_bool(
    os.getenv("ANALYZER_QUEUE_WINDOWS_SWEEP_ONLY_COMPLETE_BACKFILL"), False
)
ANALYZER_DEPENDENCY_SWEEP_PERIOD_SECONDS = int(os.getenv("ANALYZER_DEPENDENCY_SWEEP_PERIOD_SECONDS", 600))
ANALYZER_DEPENDENCY_SWEEP_MAX_PRS_PER_REPO = int(os.getenv("ANALYZER_DEPENDENCY_SWEEP_MAX_PRS_PER_REPO", 50))
ANALYZER_DEPENDENCY_SWEEP_ONLY_OPEN = env_bool(os.getenv("ANALYZER_DEPENDENCY_SWEEP_ONLY_OPEN"), False)
ANALYZER_DEPENDENCY_SWEEP_BUILDER_VERSION = int(os.getenv("ANALYZER_DEPENDENCY_SWEEP_BUILDER_VERSION", 1))
ANALYZER_DEPENDENCY_SWEEP_FANOUT = env_bool(os.getenv("ANALYZER_DEPENDENCY_SWEEP_FANOUT"), True)
ANALYZER_QUEUEBOARD_SNAPSHOT_PERIOD_SECONDS = int(os.getenv("ANALYZER_QUEUEBOARD_SNAPSHOT_PERIOD_SECONDS", 300))
ANALYZER_QUEUEBOARD_SNAPSHOT_TTL_SECONDS = int(os.getenv("ANALYZER_QUEUEBOARD_SNAPSHOT_TTL_SECONDS", 300))
ANALYZER_REVIEWER_ASSIGNMENT_PERIOD_SECONDS = int(os.getenv("ANALYZER_REVIEWER_ASSIGNMENT_PERIOD_SECONDS", 86400))
ANALYZER_REVIEWER_ASSIGNMENT_TTL_SECONDS = int(
    os.getenv("ANALYZER_REVIEWER_ASSIGNMENT_TTL_SECONDS", ANALYZER_QUEUEBOARD_SNAPSHOT_TTL_SECONDS)
)
ANALYZER_AREA_STATS_PERIOD_SECONDS = int(os.getenv("ANALYZER_AREA_STATS_PERIOD_SECONDS", 300))
ANALYZER_AREA_STATS_TTL_SECONDS = int(os.getenv("ANALYZER_AREA_STATS_TTL_SECONDS", ANALYZER_QUEUEBOARD_SNAPSHOT_TTL_SECONDS))
ANALYTICS_CONVERGENCE_PERIOD_SECONDS = int(os.getenv("ANALYTICS_CONVERGENCE_PERIOD_SECONDS", 900))

# CI filter (opt-in allowlist mode)
# Set mode to 'allowlist' to enable filtering by the following substrings; otherwise all contexts are ingested.
SYNCER_CI_FILTER_MODE = os.getenv("SYNCER_CI_FILTER_MODE", "all").lower()
# Comma-separated substrings matched case-insensitively against CheckRun.name
SYNCER_CI_ALLOW_CHECKRUN_NAMES = os.getenv("SYNCER_CI_ALLOW_CHECKRUN_NAMES", "")
# Comma-separated substrings matched case-insensitively against StatusContext.context
SYNCER_CI_ALLOW_STATUS_NAMES = os.getenv("SYNCER_CI_ALLOW_STATUS_NAMES", "")

# Beat schedule: periodically enqueue repo syncs for active repositories.
CELERY_BEAT_SCHEDULE = {}
if SYNCER_ACTIVE_REPOS_PERIOD_SECONDS > 0:
    CELERY_BEAT_SCHEDULE["sync_active_repos"] = {
        "task": "syncer.sync_active_repos",
        "schedule": SYNCER_ACTIVE_REPOS_PERIOD_SECONDS,
    }
if 900 > 0:
    CELERY_BEAT_SCHEDULE["collect_syncer_metrics"] = {
        "task": "syncer.collect_metrics",
        "schedule": 900,  # 15 minutes
    }
if SYNCER_HISTORY_BACKFILL_PERIOD_SECONDS > 0:
    CELERY_BEAT_SCHEDULE["backfill_repo_history"] = {
        "task": "syncer.backfill_repo_history_active",
        "schedule": SYNCER_HISTORY_BACKFILL_PERIOD_SECONDS,
    }
if SYNCER_INCOMPLETE_BACKFILL_PERIOD_SECONDS > 0:
    CELERY_BEAT_SCHEDULE["backfill_repo_incomplete_prs"] = {
        "task": "syncer.backfill_repo_incomplete_prs_active",
        "schedule": SYNCER_INCOMPLETE_BACKFILL_PERIOD_SECONDS,
        "kwargs": {
            "limit": SYNCER_INCOMPLETE_BACKFILL_LIMIT,
        },
    }
if SYNCER_PENDING_CI_REFRESH_PERIOD_SECONDS > 0:
    CELERY_BEAT_SCHEDULE["refresh_pending_ci_for_active_repos"] = {
        "task": "syncer.refresh_pending_ci_for_active_repos",
        "schedule": SYNCER_PENDING_CI_REFRESH_PERIOD_SECONDS,
        "kwargs": {
            "max_prs_per_repo": SYNCER_PENDING_CI_REFRESH_MAX_PRS,
            "max_shas_per_pr": SYNCER_PENDING_CI_REFRESH_MAX_SHAS_PER_PR,
            "max_pending_hours": SYNCER_PENDING_CI_MAX_AGE_HOURS,
        },
    }
if SYNCER_COMMIT_HISTORY_SWEEP_PERIOD_SECONDS > 0:
    CELERY_BEAT_SCHEDULE["harvest_commit_history"] = {
        "task": "syncer.harvest_commit_history_sweep",
        "schedule": SYNCER_COMMIT_HISTORY_SWEEP_PERIOD_SECONDS,
        "kwargs": {
            "max_jobs": SYNCER_COMMIT_HISTORY_SWEEP_MAX_JOBS,
            "max_pages": SYNCER_COMMIT_HISTORY_SWEEP_MAX_PAGES,
            "page_size": SYNCER_COMMIT_HISTORY_SWEEP_PAGE_SIZE,
        },
    }
if ANALYZER_MISSING_CI_SWEEP_PERIOD_SECONDS > 0:
    CELERY_BEAT_SCHEDULE["analyzer_missing_ci"] = {
        "task": "analyzer.plan_missing_ci",
        "schedule": ANALYZER_MISSING_CI_SWEEP_PERIOD_SECONDS,
        "kwargs": {
            "max_prs_per_repo": ANALYZER_MISSING_CI_SWEEP_MAX_PRS_PER_REPO,
            "shas_per_pr": ANALYZER_MISSING_CI_SWEEP_SHAS_PER_PR,
            "only_complete_backfill": ANALYZER_MISSING_CI_SWEEP_ONLY_COMPLETE_BACKFILL,
        },
    }
if ANALYZER_REVISION_SWEEP_PERIOD_SECONDS > 0:
    CELERY_BEAT_SCHEDULE["analyzer_revision_sweep"] = {
        "task": "analyzer.rebuild_revisions_sweep",
        "schedule": ANALYZER_REVISION_SWEEP_PERIOD_SECONDS,
        "kwargs": {
            "max_prs_per_repo": ANALYZER_REVISION_SWEEP_MAX_PRS_PER_REPO,
            "only_complete_backfill": ANALYZER_REVISION_SWEEP_ONLY_COMPLETE_BACKFILL,
        },
    }
if ANALYZER_QUEUE_WINDOWS_SWEEP_PERIOD_SECONDS > 0:
    CELERY_BEAT_SCHEDULE["analyzer_queue_windows_sweep"] = {
        "task": "analyzer.rebuild_queue_windows_sweep",
        "schedule": ANALYZER_QUEUE_WINDOWS_SWEEP_PERIOD_SECONDS,
        "kwargs": {
            "max_prs_per_repo": ANALYZER_QUEUE_WINDOWS_SWEEP_MAX_PRS_PER_REPO,
            "only_complete_backfill": ANALYZER_QUEUE_WINDOWS_SWEEP_ONLY_COMPLETE_BACKFILL,
        },
    }
if ANALYZER_DEPENDENCY_SWEEP_PERIOD_SECONDS > 0:
    CELERY_BEAT_SCHEDULE["analyzer_dependency_sweep"] = {
        "task": "analyzer.rebuild_dependencies_sweep",
        "schedule": ANALYZER_DEPENDENCY_SWEEP_PERIOD_SECONDS,
        "kwargs": {
            "max_prs_per_repo": ANALYZER_DEPENDENCY_SWEEP_MAX_PRS_PER_REPO,
            "only_open": ANALYZER_DEPENDENCY_SWEEP_ONLY_OPEN,
            "builder_version": ANALYZER_DEPENDENCY_SWEEP_BUILDER_VERSION,
            "fanout": ANALYZER_DEPENDENCY_SWEEP_FANOUT,
        },
    }
if ANALYTICS_CONVERGENCE_PERIOD_SECONDS > 0:
    CELERY_BEAT_SCHEDULE["collect_convergence"] = {
        "task": "syncer.collect_convergence",
        "schedule": ANALYTICS_CONVERGENCE_PERIOD_SECONDS,
    }
    CELERY_BEAT_SCHEDULE["collect_analyzer_convergence"] = {
        "task": "analyzer.collect_convergence",
        "schedule": ANALYTICS_CONVERGENCE_PERIOD_SECONDS,
    }
# Optional engagement backfill; disable by setting SYNCER_ENGAGEMENT_BACKFILL_PERIOD_SECONDS<=0
if SYNCER_ENGAGEMENT_BACKFILL_PERIOD_SECONDS > 0:
    CELERY_BEAT_SCHEDULE["backfill_repo_engagement"] = {
        "task": "syncer.backfill_repo_engagement_active",
        "schedule": SYNCER_ENGAGEMENT_BACKFILL_PERIOD_SECONDS,
        "kwargs": {
            "limit": SYNCER_ENGAGEMENT_BACKFILL_LIMIT,
        },
    }
if ANALYZER_QUEUEBOARD_SNAPSHOT_PERIOD_SECONDS > 0:
    CELERY_BEAT_SCHEDULE["refresh_queueboard_snapshots"] = {
        "task": "analyzer.refresh_queueboard_snapshots",
        "schedule": ANALYZER_QUEUEBOARD_SNAPSHOT_PERIOD_SECONDS,
        "kwargs": {
            "cache_key": "default",
            "fanout": True,
        },
    }
if ANALYZER_REVIEWER_ASSIGNMENT_PERIOD_SECONDS > 0:
    CELERY_BEAT_SCHEDULE["refresh_reviewer_assignments"] = {
        "task": "analyzer.refresh_reviewer_assignments",
        "schedule": ANALYZER_REVIEWER_ASSIGNMENT_PERIOD_SECONDS,
        "kwargs": {
            "cache_key": "default",
            "fanout": True,
        },
    }
if ANALYZER_AREA_STATS_PERIOD_SECONDS > 0:
    CELERY_BEAT_SCHEDULE["refresh_area_stats"] = {
        "task": "analyzer.refresh_area_stats",
        "schedule": ANALYZER_AREA_STATS_PERIOD_SECONDS,
        "kwargs": {
            "cache_key": "default",
            "fanout": True,
        },
    }
