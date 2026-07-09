"""Base settings shared across all environments."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from celery.schedules import crontab

BASE_DIR = Path(__file__).resolve().parent.parent.parent
APPS_DIR = Path(__file__).resolve().parent.parent

SRC_DIR = BASE_DIR / "src"
if SRC_DIR.exists() and str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def env_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.lower() in {"1", "true", "t", "yes", "y"}


def env_optional_bounded_int(name: str, *, minimum: int, maximum: int) -> int | None:
    raw = os.getenv(name)
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer in [{minimum}, {maximum}]") from exc
    if parsed < minimum or parsed > maximum:
        raise RuntimeError(f"{name} must be in [{minimum}, {maximum}]")
    return parsed


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
    "console",
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

# Canonical public base URL of the queueboard Django site (scheme://host, no trailing path).
# Feature link-builders (reviewer console, Zulip prefs/registration deep-links) fall back to this,
# so a deployment can set one variable instead of several. NOTE: this is the web app, NOT the Zulip
# chat server (ZULIP_BASE_URL). Leave empty only in local dev. Resolve via
# core.services.site_urls.resolve_site_base_url() rather than reading either setting directly.
QUEUEBOARD_BASE_URL = os.getenv("QUEUEBOARD_BASE_URL", "").strip().rstrip("/")

ZULIP_WEBHOOK_TOKEN = os.getenv("ZULIP_WEBHOOK_TOKEN")
ZULIP_BASE_URL = os.getenv("ZULIP_BASE_URL", "")
ZULIP_BOT_EMAIL = os.getenv("ZULIP_BOT_EMAIL", "")
ZULIP_BOT_API_KEY = os.getenv("ZULIP_BOT_API_KEY", "")
ZULIP_USER_EMAIL = os.getenv("ZULIP_USER_EMAIL", "")
ZULIP_USER_API_KEY = os.getenv("ZULIP_USER_API_KEY", "")
# Back-compat: existing deployments set this directly. When unset it falls back to the canonical
# QUEUEBOARD_BASE_URL so a single variable configures every deep-link base.
ZULIP_PREFS_URL_BASE = os.getenv("ZULIP_PREFS_URL_BASE", "").strip().rstrip("/") or QUEUEBOARD_BASE_URL
ZULIP_PREFS_TOKEN_SECRET = os.getenv("ZULIP_PREFS_TOKEN_SECRET", "")
ZULIP_PREFS_TOKEN_SALT = os.getenv("ZULIP_PREFS_TOKEN_SALT", "zulip_bot.prefs")
ZULIP_PREFS_TOKEN_TTL_SECONDS = int(os.getenv("ZULIP_PREFS_TOKEN_TTL_SECONDS", 1800))
ZULIP_REGISTRATION_TOKEN_SALT = os.getenv("ZULIP_REGISTRATION_TOKEN_SALT", "zulip_bot.registration")
ZULIP_REGISTRATION_TOKEN_TTL_SECONDS = int(os.getenv("ZULIP_REGISTRATION_TOKEN_TTL_SECONDS", 1800))
ZULIP_REGISTRATION_OAUTH_STATE_SALT = os.getenv("ZULIP_REGISTRATION_OAUTH_STATE_SALT", "zulip_bot.registration.oauth_state")
ZULIP_REGISTRATION_OAUTH_STATE_TTL_SECONDS = int(os.getenv("ZULIP_REGISTRATION_OAUTH_STATE_TTL_SECONDS", 600))
ZULIP_CLOSE_PR_TOKEN_SECRET = os.getenv("ZULIP_CLOSE_PR_TOKEN_SECRET", "")
ZULIP_CLOSE_PR_TOKEN_SALT = os.getenv("ZULIP_CLOSE_PR_TOKEN_SALT", "zulip_bot.close_pr")
ZULIP_CLOSE_PR_TOKEN_TTL_SECONDS = int(os.getenv("ZULIP_CLOSE_PR_TOKEN_TTL_SECONDS", 1800))
ZULIP_LABEL_PR_TOKEN_SECRET = os.getenv("ZULIP_LABEL_PR_TOKEN_SECRET", "")
ZULIP_LABEL_PR_TOKEN_SALT = os.getenv("ZULIP_LABEL_PR_TOKEN_SALT", "zulip_bot.label_pr")
ZULIP_LABEL_PR_TOKEN_TTL_SECONDS = int(os.getenv("ZULIP_LABEL_PR_TOKEN_TTL_SECONDS", 1800))
ZULIP_ASSIGNMENT_SUCCESS_EMOJI = os.getenv("ZULIP_ASSIGNMENT_SUCCESS_EMOJI", "thumbs_up")
# Feature flags: keep in sync with .env.example — a missing entry here silently disables the feature.
ZULIP_ASSIGNMENT_MUTATIONS_ENABLED = os.getenv("ZULIP_ASSIGNMENT_MUTATIONS_ENABLED", "")
ZULIP_CLOSE_PR_MUTATIONS_ENABLED = os.getenv("ZULIP_CLOSE_PR_MUTATIONS_ENABLED", "")
ZULIP_LABEL_PR_MUTATIONS_ENABLED = os.getenv("ZULIP_LABEL_PR_MUTATIONS_ENABLED", "")
GITHUB_OAUTH_CLIENT_ID = os.getenv("GITHUB_OAUTH_CLIENT_ID", "")
GITHUB_OAUTH_CLIENT_SECRET = os.getenv("GITHUB_OAUTH_CLIENT_SECRET", "")
GITHUB_OAUTH_REDIRECT_URI = os.getenv("GITHUB_OAUTH_REDIRECT_URI", "")
GITHUB_OAUTH_AUTHORIZE_URL = os.getenv("GITHUB_OAUTH_AUTHORIZE_URL", "https://github.com/login/oauth/authorize")
GITHUB_OAUTH_TOKEN_URL = os.getenv("GITHUB_OAUTH_TOKEN_URL", "https://github.com/login/oauth/access_token")
GITHUB_API_URL = os.getenv("GITHUB_API_URL", "https://api.github.com")
GITHUB_OAUTH_SCOPE = os.getenv("GITHUB_OAUTH_SCOPE", "read:user")
# Reviewer console (design doc 050): TTL for the signed OAuth `state` round-trip (sign-in click ->
# GitHub -> callback). Ten minutes is plenty; the per-session CSRF nonce is the real guard.
CONSOLE_OAUTH_STATE_TTL_SECONDS = int(os.getenv("CONSOLE_OAUTH_STATE_TTL_SECONDS", 600))
GITHUB_WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")
GITHUB_APP_TOKEN_CONFIG: dict[str, object] = {}
_GITHUB_APP_TOKEN_CONFIG_ENV = os.getenv("GITHUB_APP_TOKEN_CONFIG", "").strip()
if _GITHUB_APP_TOKEN_CONFIG_ENV:
    parsed_app_token_config = json.loads(_GITHUB_APP_TOKEN_CONFIG_ENV)
    if not isinstance(parsed_app_token_config, dict):
        raise RuntimeError("GITHUB_APP_TOKEN_CONFIG env var must be a JSON object")
    GITHUB_APP_TOKEN_CONFIG = parsed_app_token_config
ZULIP_COMMAND_POLICY: dict[str, dict[str, list[int | str]]] = {}
_ZULIP_COMMAND_POLICY_ENV = os.getenv("ZULIP_COMMAND_POLICY", "").strip()
if _ZULIP_COMMAND_POLICY_ENV:
    parsed_policy = json.loads(_ZULIP_COMMAND_POLICY_ENV)
    if not isinstance(parsed_policy, dict):
        raise RuntimeError("ZULIP_COMMAND_POLICY env var must be a JSON object")
    ZULIP_COMMAND_POLICY = parsed_policy
ZULIP_REPO_LOG: dict[str, dict[str, str]] = {}
_ZULIP_REPO_LOG_ENV = os.getenv("ZULIP_REPO_LOG", "").strip()
if _ZULIP_REPO_LOG_ENV:
    parsed_repo_log = json.loads(_ZULIP_REPO_LOG_ENV)
    if not isinstance(parsed_repo_log, dict):
        raise RuntimeError("ZULIP_REPO_LOG env var must be a JSON object")
    ZULIP_REPO_LOG = parsed_repo_log


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
        "syncer.upgrade_schema_versions": {"queue": SYNCER_GITHUB_QUEUE},
        "syncer.upgrade_schema_versions_active": {"queue": SYNCER_GITHUB_QUEUE},
        "syncer.harvest_commit_history": {"queue": SYNCER_GITHUB_QUEUE},
        "syncer.harvest_commit_history_sweep": {"queue": SYNCER_GITHUB_QUEUE},
        "syncer.sync_label_catalog": {"queue": SYNCER_GITHUB_QUEUE},
        "syncer.sync_label_catalog_for_active_repos": {"queue": SYNCER_GITHUB_QUEUE},
    }

# Syncer scheduling defaults (env-overridable)
SYNCER_DISCOVERY_LOOKBACK_MINUTES = int(os.getenv("SYNCER_DISCOVERY_LOOKBACK_MINUTES", 60))
SYNCER_DISCOVERY_LIMIT = int(os.getenv("SYNCER_DISCOVERY_LIMIT", 100))
SYNCER_DISCOVERY_OVERLAP_SECONDS = int(os.getenv("SYNCER_DISCOVERY_OVERLAP_SECONDS", 300))
SYNCER_DISCOVERY_CONTINUATION_DELAY_SECONDS = int(os.getenv("SYNCER_DISCOVERY_CONTINUATION_DELAY_SECONDS", 5))
SYNCER_DISCOVERY_STATES_DEFAULT = [
    s.strip().upper() for s in os.getenv("SYNCER_DISCOVERY_STATES_DEFAULT", "OPEN,MERGED,CLOSED").split(",") if s.strip()
]
SYNCER_GITHUB_WEBHOOK_ENABLED = env_bool(os.getenv("SYNCER_GITHUB_WEBHOOK_ENABLED"), False)
SYNCER_GITHUB_WEBHOOK_DRY_RUN = env_bool(os.getenv("SYNCER_GITHUB_WEBHOOK_DRY_RUN"), False)
# Rate and paging defaults
SYNCER_RATE_REMAINING_MIN = int(os.getenv("SYNCER_RATE_REMAINING_MIN", 200))
SYNCER_TIMELINE_K_DEFAULT = int(os.getenv("SYNCER_TIMELINE_K_DEFAULT", 150))
SYNCER_COMMITS_M_DEFAULT = int(os.getenv("SYNCER_COMMITS_M_DEFAULT", 15))
# Per-review inline-comment fetch cap on PullRequestReview.comments(first: K).
# Reviews exceeding this cap get a PRReviewInlineCommentBackfill row for the v3
# recovery sweep; see docs/design-decisions/044-….
SYNCER_INLINE_COMMENTS_PER_REVIEW = int(os.getenv("SYNCER_INLINE_COMMENTS_PER_REVIEW", 20))
SYNCER_LAST_SYNC_EPSILON_SECONDS = int(os.getenv("SYNCER_LAST_SYNC_EPSILON_SECONDS", 300))
SYNCER_ACTIVE_REPOS_PERIOD_SECONDS = int(os.getenv("SYNCER_ACTIVE_REPOS_PERIOD_SECONDS", 300))
SYNCER_REPO_ENQUEUE_BATCH_MAX = int(os.getenv("SYNCER_REPO_ENQUEUE_BATCH_MAX", 30))
SYNCER_EST_COST_PER_PR = int(os.getenv("SYNCER_EST_COST_PER_PR", 150))
SYNCER_TIMELINE_BACKFILL_PAGES = int(os.getenv("SYNCER_TIMELINE_BACKFILL_PAGES", 2))
# Commit backfill per up-to-date run (pages of the commits connection to walk backward)
SYNCER_COMMITS_BACKFILL_PAGES = int(os.getenv("SYNCER_COMMITS_BACKFILL_PAGES", 2))
SYNCER_CI_BY_SHA_PAGES = int(os.getenv("SYNCER_CI_BY_SHA_PAGES", 1))
SYNCER_SYNC_CI_DEDUPE_TTL_SECONDS = int(os.getenv("SYNCER_SYNC_CI_DEDUPE_TTL_SECONDS", 300))
SYNCER_SYNC_PR_DEDUPE_TTL_SECONDS = int(os.getenv("SYNCER_SYNC_PR_DEDUPE_TTL_SECONDS", 300))
SYNCER_SYNC_PR_RUNTIME_DEDUPE_TTL_SECONDS = int(os.getenv("SYNCER_SYNC_PR_RUNTIME_DEDUPE_TTL_SECONDS", 300))
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
# Sync schema upgrader pacing (see syncer/services/sync_schema_upgrades.py).
# BATCH_SIZE bounds DB-only stamping work per task invocation; KICK_LIMIT bounds
# GitHub-bound sync_pr_task enqueues per invocation so a wave can't outrun the
# rate-limit budget. Period <= 0 disables the periodic beat.
SYNCER_SCHEMA_UPGRADE_PERIOD_SECONDS = int(os.getenv("SYNCER_SCHEMA_UPGRADE_PERIOD_SECONDS", 600))
SYNCER_SCHEMA_UPGRADE_BATCH_SIZE = int(os.getenv("SYNCER_SCHEMA_UPGRADE_BATCH_SIZE", 1000))
SYNCER_SCHEMA_UPGRADE_KICK_LIMIT = int(os.getenv("SYNCER_SCHEMA_UPGRADE_KICK_LIMIT", 20))
# Optional gate that lets a deploy hold the wave below CURRENT_SYNC_SCHEMA_VERSION
# (e.g. ship the v2 code with the gate=1 in staging, then flip to 2 to fire). Unset
# / empty / non-int values mean "use CURRENT_SYNC_SCHEMA_VERSION". Always clamped
# to the constant by services/sync_schema_upgrades.effective_target_version.
_SYNCER_SCHEMA_UPGRADE_TARGET_VERSION_RAW = os.getenv("SYNCER_SCHEMA_UPGRADE_TARGET_VERSION", "")
try:
    SYNCER_SCHEMA_UPGRADE_TARGET_VERSION: int | None = (
        int(_SYNCER_SCHEMA_UPGRADE_TARGET_VERSION_RAW) if _SYNCER_SCHEMA_UPGRADE_TARGET_VERSION_RAW else None
    )
except ValueError:
    SYNCER_SCHEMA_UPGRADE_TARGET_VERSION = None

# Pending-CI refresh defaults
SYNCER_PENDING_CI_MAX_AGE_HOURS = int(os.getenv("SYNCER_PENDING_CI_MAX_AGE_HOURS", 48))
SYNCER_PENDING_CI_REFRESH_PERIOD_SECONDS = int(os.getenv("SYNCER_PENDING_CI_REFRESH_PERIOD_SECONDS", 600))
SYNCER_PENDING_CI_REFRESH_MAX_PRS = int(os.getenv("SYNCER_PENDING_CI_REFRESH_MAX_PRS", 5))
SYNCER_PENDING_CI_REFRESH_MAX_SHAS_PER_PR = int(os.getenv("SYNCER_PENDING_CI_REFRESH_MAX_SHAS_PER_PR", 5))

# CI row expiry / superseded-row cleanup
SYNCER_CI_EXPIRY_PERIOD_SECONDS = int(os.getenv("SYNCER_CI_EXPIRY_PERIOD_SECONDS", 86400))
SYNCER_CI_STALE_PENDING_DAYS = int(os.getenv("SYNCER_CI_STALE_PENDING_DAYS", 30))
# Per-statement Postgres timeout inside the expiry task; a runaway plan once ran for
# days and blocked vacuum database-wide. 0 disables the guard.
SYNCER_CI_EXPIRY_STATEMENT_TIMEOUT_SECONDS = int(os.getenv("SYNCER_CI_EXPIRY_STATEMENT_TIMEOUT_SECONDS", 300))

# Webhook delivery log cleanup
SYNCER_WEBHOOK_DELIVERY_RETENTION_DAYS = int(os.getenv("SYNCER_WEBHOOK_DELIVERY_RETENTION_DAYS", 7))
SYNCER_WEBHOOK_DELIVERY_CLEANUP_PERIOD_SECONDS = int(os.getenv("SYNCER_WEBHOOK_DELIVERY_CLEANUP_PERIOD_SECONDS", 86400))

# Repo label-catalog refresh: how often to reconcile LabelDef against GitHub for active repos.
# Labels change infrequently, so an hourly cadence is plenty. Set to 0 to disable.
SYNCER_LABEL_CATALOG_PERIOD_SECONDS = int(os.getenv("SYNCER_LABEL_CATALOG_PERIOD_SECONDS", 3600))

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
ANALYZER_PENDING_STATUS_STALE_NON_OPEN_HOURS = int(os.getenv("ANALYZER_PENDING_STATUS_STALE_NON_OPEN_HOURS", 8))
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
# Reviewer assignment compute refresh. PERIOD_SECONDS > 0 enables scheduling (0 disables);
# it runs daily at ANALYZER_REVIEWER_ASSIGNMENT_UTC_HOUR:MINUTE (default 00:30 UTC).
ANALYZER_REVIEWER_ASSIGNMENT_PERIOD_SECONDS = int(os.getenv("ANALYZER_REVIEWER_ASSIGNMENT_PERIOD_SECONDS", 86400))
ANALYZER_REVIEWER_ASSIGNMENT_TTL_SECONDS = int(
    os.getenv("ANALYZER_REVIEWER_ASSIGNMENT_TTL_SECONDS", ANALYZER_QUEUEBOARD_SNAPSHOT_TTL_SECONDS)
)
ANALYZER_REVIEWER_ASSIGNMENT_UTC_HOUR = env_optional_bounded_int(
    "ANALYZER_REVIEWER_ASSIGNMENT_UTC_HOUR",
    minimum=0,
    maximum=23,
)
ANALYZER_REVIEWER_ASSIGNMENT_UTC_MINUTE = env_optional_bounded_int(
    "ANALYZER_REVIEWER_ASSIGNMENT_UTC_MINUTE",
    minimum=0,
    maximum=59,
)
# Applying proposed reviewer assignments to GitHub (design doc 046).
# ENABLED actually POSTs assignees; DRY_RUN computes + records outcomes without mutating.
ANALYZER_REVIEWER_ASSIGNMENT_APPLY_ENABLED = env_bool(os.getenv("ANALYZER_REVIEWER_ASSIGNMENT_APPLY_ENABLED"), False)
ANALYZER_REVIEWER_ASSIGNMENT_APPLY_DRY_RUN = env_bool(os.getenv("ANALYZER_REVIEWER_ASSIGNMENT_APPLY_DRY_RUN"), False)
ANALYZER_REVIEWER_ASSIGNMENT_APPLY_PERIOD_SECONDS = int(os.getenv("ANALYZER_REVIEWER_ASSIGNMENT_APPLY_PERIOD_SECONDS", 86400))
ANALYZER_REVIEWER_ASSIGNMENT_APPLY_UTC_HOUR = env_optional_bounded_int(
    "ANALYZER_REVIEWER_ASSIGNMENT_APPLY_UTC_HOUR",
    minimum=0,
    maximum=23,
)
ANALYZER_REVIEWER_ASSIGNMENT_APPLY_UTC_MINUTE = env_optional_bounded_int(
    "ANALYZER_REVIEWER_ASSIGNMENT_APPLY_UTC_MINUTE",
    minimum=0,
    maximum=59,
)
# Skip snapshots older than this many hours (guards against acting on stale compute).
ANALYZER_REVIEWER_ASSIGNMENT_APPLY_MAX_AGE_HOURS = int(os.getenv("ANALYZER_REVIEWER_ASSIGNMENT_APPLY_MAX_AGE_HOURS", 48))
# Do not re-apply the same (PR, reviewer) within this many days (sync-lag dedupe window).
ANALYZER_REVIEWER_ASSIGNMENT_APPLY_DEDUPE_DAYS = int(os.getenv("ANALYZER_REVIEWER_ASSIGNMENT_APPLY_DEDUPE_DAYS", 7))
# Cap GitHub assignment mutations per repo per run (0 = unlimited). A conservative
# non-zero default bounds GitHub secondary-rate-limit exposure and drains any cutover
# backlog gradually; capped-over proposals are left for the next run.
ANALYZER_REVIEWER_ASSIGNMENT_APPLY_MAX_PER_REPO = int(os.getenv("ANALYZER_REVIEWER_ASSIGNMENT_APPLY_MAX_PER_REPO", 25))
# Reviewer assignment acceptance gate (design doc 050) — builder/engine tuning.
# A pending (proposed) proposal contributes this weighted load to the reviewer (a proposal
# occupies a slot, like an AwaitingReview PR) and excludes the PR from re-proposal.
ANALYZER_ASSIGNMENT_PROPOSAL_PENDING_LOAD_WEIGHT = float(os.getenv("ANALYZER_ASSIGNMENT_PROPOSAL_PENDING_LOAD_WEIGHT", "1.0"))
# Soft cooldown: skip a reviewer for a PR when a proposal for it expired (silent timeout)
# within this many days. Not a permanent opt-out (that is an explicit decline). 0 disables it.
ANALYZER_ASSIGNMENT_PROPOSAL_EXPIRE_COOLDOWN_DAYS = int(os.getenv("ANALYZER_ASSIGNMENT_PROPOSAL_EXPIRE_COOLDOWN_DAYS", "14"))
# Acceptance-gate rollout flags (design doc 050), each independently toggleable like doc 028's
# propose -> deliver -> assign-on-accept discipline. All default off so the gate is inert until
# an operator opts in.
#   ENABLED            master kill switch: the propose task creates proposals / direct-assigns.
#   DELIVERY_ENABLED   send the per-reviewer proposal digest DM (consumed in Chunk 5).
#   ASSIGN_ON_ACCEPT_ENABLED  the console accept handler performs the GitHub assign (Chunk 6).
#   DRY_RUN            propose computes + records would-do outcomes without any side effect.
# Enable EITHER this gate OR the legacy ANALYZER_REVIEWER_ASSIGNMENT_APPLY_* task, not both:
# propose supersedes apply (it direct-assigns auto-mode reviewers itself and proposes to the rest).
# Enforced in code: when both are enabled, the apply task skips itself (logging an error) so the
# proposal-unaware path cannot bypass the gate.
ANALYZER_ASSIGNMENT_PROPOSALS_ENABLED = env_bool(os.getenv("ANALYZER_ASSIGNMENT_PROPOSALS_ENABLED"), False)
ANALYZER_ASSIGNMENT_PROPOSALS_DELIVERY_ENABLED = env_bool(os.getenv("ANALYZER_ASSIGNMENT_PROPOSALS_DELIVERY_ENABLED"), False)
ANALYZER_ASSIGNMENT_PROPOSALS_ASSIGN_ON_ACCEPT_ENABLED = env_bool(
    os.getenv("ANALYZER_ASSIGNMENT_PROPOSALS_ASSIGN_ON_ACCEPT_ENABLED"), False
)
ANALYZER_ASSIGNMENT_PROPOSALS_DRY_RUN = env_bool(os.getenv("ANALYZER_ASSIGNMENT_PROPOSALS_DRY_RUN"), False)
# Acceptance window: a proposal expires this many days after creation unless accepted. The
# per-reviewer override in ReviewerPreference.notification_settings is clamped to >= 7.
ANALYZER_ASSIGNMENT_PROPOSAL_WINDOW_DAYS = int(os.getenv("ANALYZER_ASSIGNMENT_PROPOSAL_WINDOW_DAYS", "7"))
# On-queue-exit policy read inside the proposal_validity predicate: "invalidate" (default) marks a
# pending proposal superseded when its PR leaves the review queue; "retain" lets it ride.
ANALYZER_ASSIGNMENT_PROPOSAL_ON_QUEUE_EXIT = os.getenv("ANALYZER_ASSIGNMENT_PROPOSAL_ON_QUEUE_EXIT", "invalidate").strip().lower()
# Propose task schedule (daily, shortly after the compute refresh at 00:30 and in place of the
# legacy apply at 00:45). PERIOD_SECONDS <= 0 disables scheduling; the task is also gated by
# ANALYZER_ASSIGNMENT_PROPOSALS_ENABLED (+ dry-run), so scheduling it while off is a cheap no-op.
ANALYZER_ASSIGNMENT_PROPOSE_PERIOD_SECONDS = int(os.getenv("ANALYZER_ASSIGNMENT_PROPOSE_PERIOD_SECONDS", 86400))
ANALYZER_ASSIGNMENT_PROPOSE_UTC_HOUR = env_optional_bounded_int(
    "ANALYZER_ASSIGNMENT_PROPOSE_UTC_HOUR",
    minimum=0,
    maximum=23,
)
ANALYZER_ASSIGNMENT_PROPOSE_UTC_MINUTE = env_optional_bounded_int(
    "ANALYZER_ASSIGNMENT_PROPOSE_UTC_MINUTE",
    minimum=0,
    maximum=59,
)
# Expiry/reconcile sweep schedule. This is essential maintenance (expire timed-out proposals,
# supersede those whose PR left the queue) and is intentionally NOT gated by the master switch,
# so flipping the gate off lets existing proposals drain. PERIOD_SECONDS <= 0 disables it.
ANALYZER_ASSIGNMENT_PROPOSAL_EXPIRY_PERIOD_SECONDS = int(os.getenv("ANALYZER_ASSIGNMENT_PROPOSAL_EXPIRY_PERIOD_SECONDS", 3600))
# Delivery task schedule (daily; default 01:00 UTC, shortly after propose creates the day's
# proposals at 00:45). PERIOD_SECONDS <= 0 disables scheduling. Actual sending is additionally
# gated inside the task by ANALYZER_ASSIGNMENT_PROPOSALS_ENABLED AND _DELIVERY_ENABLED (+ dry-run),
# so scheduling it while off is a cheap no-op (feature_disabled).
ANALYZER_ASSIGNMENT_DELIVER_PERIOD_SECONDS = int(os.getenv("ANALYZER_ASSIGNMENT_DELIVER_PERIOD_SECONDS", 86400))
ANALYZER_ASSIGNMENT_DELIVER_UTC_HOUR = env_optional_bounded_int(
    "ANALYZER_ASSIGNMENT_DELIVER_UTC_HOUR",
    minimum=0,
    maximum=23,
)
ANALYZER_ASSIGNMENT_DELIVER_UTC_MINUTE = env_optional_bounded_int(
    "ANALYZER_ASSIGNMENT_DELIVER_UTC_MINUTE",
    minimum=0,
    maximum=59,
)
ANALYZER_REVIEWER_ATTENTION_ENABLED = env_bool(os.getenv("ANALYZER_REVIEWER_ATTENTION_ENABLED"), False)
ANALYZER_REVIEWER_ATTENTION_ENFORCEMENT_ENABLED = env_bool(
    os.getenv("ANALYZER_REVIEWER_ATTENTION_ENFORCEMENT_ENABLED"),
    False,
)
ANALYZER_REVIEWER_ATTENTION_DELIVERY_ENABLED = env_bool(
    os.getenv("ANALYZER_REVIEWER_ATTENTION_DELIVERY_ENABLED"),
    False,
)
ANALYZER_REVIEWER_ATTENTION_PERIOD_SECONDS = int(os.getenv("ANALYZER_REVIEWER_ATTENTION_PERIOD_SECONDS", 86400))
ANALYZER_REVIEWER_ATTENTION_UTC_HOUR = env_optional_bounded_int(
    "ANALYZER_REVIEWER_ATTENTION_UTC_HOUR",
    minimum=0,
    maximum=23,
)
ANALYZER_REVIEWER_ATTENTION_UTC_MINUTE = env_optional_bounded_int(
    "ANALYZER_REVIEWER_ATTENTION_UTC_MINUTE",
    minimum=0,
    maximum=59,
)
ANALYZER_REVIEWER_ATTENTION_POLICY_START_AT = os.getenv("ANALYZER_REVIEWER_ATTENTION_POLICY_START_AT")
ANALYZER_REVIEWER_ATTENTION_CLEANUP_DAY_OF_WEEK = (
    os.getenv("ANALYZER_REVIEWER_ATTENTION_CLEANUP_DAY_OF_WEEK", "sun").strip() or "sun"
)
ANALYZER_REVIEWER_ATTENTION_CLEANUP_UTC_HOUR = env_optional_bounded_int(
    "ANALYZER_REVIEWER_ATTENTION_CLEANUP_UTC_HOUR",
    minimum=0,
    maximum=23,
)
ANALYZER_REVIEWER_ATTENTION_CLEANUP_UTC_MINUTE = env_optional_bounded_int(
    "ANALYZER_REVIEWER_ATTENTION_CLEANUP_UTC_MINUTE",
    minimum=0,
    maximum=59,
)
ANALYZER_REVIEWER_ATTENTION_NOTIFICATION_RETENTION_DAYS = int(
    os.getenv("ANALYZER_REVIEWER_ATTENTION_NOTIFICATION_RETENTION_DAYS", 30)
)
ANALYZER_REVIEWER_ATTENTION_AUTO_UNASSIGN_RETENTION_DAYS = int(
    os.getenv("ANALYZER_REVIEWER_ATTENTION_AUTO_UNASSIGN_RETENTION_DAYS", 90)
)
ANALYZER_REVIEWER_ATTENTION_RUN_RETENTION_DAYS = int(os.getenv("ANALYZER_REVIEWER_ATTENTION_RUN_RETENTION_DAYS", 30))
ANALYZER_AREA_STATS_PERIOD_SECONDS = int(os.getenv("ANALYZER_AREA_STATS_PERIOD_SECONDS", 300))
ANALYZER_AREA_STATS_TTL_SECONDS = int(os.getenv("ANALYZER_AREA_STATS_TTL_SECONDS", ANALYZER_QUEUEBOARD_SNAPSHOT_TTL_SECONDS))
ANALYTICS_CONVERGENCE_PERIOD_SECONDS = int(os.getenv("ANALYTICS_CONVERGENCE_PERIOD_SECONDS", 900))

# Archive backfill importer (design doc 043). Master flag defaults to False;
# bootstrap command enrolls a worklist before the scheduler tick is enabled.
ARCHIVE_IMPORT_ENABLED = env_bool(os.getenv("ARCHIVE_IMPORT_ENABLED"), False)
ARCHIVE_IMPORT_BATCH_SIZE = int(os.getenv("ARCHIVE_IMPORT_BATCH_SIZE", 10))
ARCHIVE_IMPORT_TICK_SECONDS = int(os.getenv("ARCHIVE_IMPORT_TICK_SECONDS", 60))
ARCHIVE_IMPORT_RAW_BASE_URL = os.getenv("ARCHIVE_IMPORT_RAW_BASE_URL", "https://raw.githubusercontent.com")
ARCHIVE_IMPORT_FETCH_TIMEOUT_SECONDS = int(os.getenv("ARCHIVE_IMPORT_FETCH_TIMEOUT_SECONDS", 30))
ARCHIVE_IMPORT_MAX_TRANSIENT_ATTEMPTS = int(os.getenv("ARCHIVE_IMPORT_MAX_TRANSIENT_ATTEMPTS", 5))

# Forced-resync drain for archive-touched live PRs (design doc 043 follow-up).
# PER_TICK gates activity inside the beat task (0 = disabled, the default), so
# operators enable/disable the drain via env var. MIN_RATE_REMAINING makes a
# tick skip enqueueing when the cached GraphQL budget is below the floor.
ARCHIVE_RESYNC_PER_TICK = int(os.getenv("ARCHIVE_RESYNC_PER_TICK", 0))
ARCHIVE_RESYNC_TICK_SECONDS = int(os.getenv("ARCHIVE_RESYNC_TICK_SECONDS", 600))
ARCHIVE_RESYNC_MIN_RATE_REMAINING = int(os.getenv("ARCHIVE_RESYNC_MIN_RATE_REMAINING", 2500))

# CI filter (opt-in allowlist mode)
# Set mode to 'allowlist' to enable filtering by the following substrings; otherwise all contexts are ingested.
SYNCER_CI_FILTER_MODE = os.getenv("SYNCER_CI_FILTER_MODE", "all").lower()
# Comma-separated substrings matched case-insensitively against commit check run names
SYNCER_CI_ALLOW_CHECKRUN_NAMES = os.getenv("SYNCER_CI_ALLOW_CHECKRUN_NAMES", "")
# Comma-separated substrings matched case-insensitively against commit status context names
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
if SYNCER_WEBHOOK_DELIVERY_CLEANUP_PERIOD_SECONDS > 0:
    CELERY_BEAT_SCHEDULE["expire_old_webhook_deliveries"] = {
        "task": "syncer.expire_old_webhook_deliveries",
        "schedule": SYNCER_WEBHOOK_DELIVERY_CLEANUP_PERIOD_SECONDS,
        "kwargs": {
            "retention_days": SYNCER_WEBHOOK_DELIVERY_RETENTION_DAYS,
        },
    }
if SYNCER_CI_EXPIRY_PERIOD_SECONDS > 0:
    CELERY_BEAT_SCHEDULE["expire_stale_ci_for_active_repos"] = {
        "task": "syncer.expire_stale_ci_for_active_repos",
        "schedule": SYNCER_CI_EXPIRY_PERIOD_SECONDS,
        "kwargs": {
            "stale_pending_days": SYNCER_CI_STALE_PENDING_DAYS,
        },
    }
if SYNCER_LABEL_CATALOG_PERIOD_SECONDS > 0:
    CELERY_BEAT_SCHEDULE["sync_label_catalog_for_active_repos"] = {
        "task": "syncer.sync_label_catalog_for_active_repos",
        "schedule": SYNCER_LABEL_CATALOG_PERIOD_SECONDS,
    }
if ARCHIVE_IMPORT_TICK_SECONDS > 0:
    # Beat fires unconditionally; ``ARCHIVE_IMPORT_ENABLED`` gates activity
    # inside the task so operators can toggle without restarting beat.
    CELERY_BEAT_SCHEDULE["archive_import_tick"] = {
        "task": "syncer.archive_import_tick",
        "schedule": ARCHIVE_IMPORT_TICK_SECONDS,
    }
if ARCHIVE_RESYNC_TICK_SECONDS > 0:
    # Beat fires unconditionally; ``ARCHIVE_RESYNC_PER_TICK`` gates activity
    # inside the task so operators can toggle without restarting beat.
    CELERY_BEAT_SCHEDULE["resync_archive_touched_tick"] = {
        "task": "syncer.resync_archive_touched_tick",
        "schedule": ARCHIVE_RESYNC_TICK_SECONDS,
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
        "options": {"headers": {"qb_enqueue_source": "beat_syncer_convergence"}},
    }
    CELERY_BEAT_SCHEDULE["collect_analyzer_convergence"] = {
        "task": "analyzer.collect_convergence",
        "schedule": ANALYTICS_CONVERGENCE_PERIOD_SECONDS,
        "options": {"headers": {"qb_enqueue_source": "beat_analyzer_convergence"}},
    }
# Sync schema upgrader; disable by setting SYNCER_SCHEMA_UPGRADE_PERIOD_SECONDS<=0
if SYNCER_SCHEMA_UPGRADE_PERIOD_SECONDS > 0:
    CELERY_BEAT_SCHEDULE["upgrade_schema_versions"] = {
        "task": "syncer.upgrade_schema_versions_active",
        "schedule": SYNCER_SCHEMA_UPGRADE_PERIOD_SECONDS,
        "kwargs": {
            "batch_size": SYNCER_SCHEMA_UPGRADE_BATCH_SIZE,
            "kick_limit": SYNCER_SCHEMA_UPGRADE_KICK_LIMIT,
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
# Compute refresh runs daily at a fixed UTC clock time (default 00:30) so the apply step
# downstream has a fresh snapshot. PERIOD_SECONDS <= 0 disables scheduling.
if ANALYZER_REVIEWER_ASSIGNMENT_PERIOD_SECONDS > 0:
    CELERY_BEAT_SCHEDULE["refresh_reviewer_assignments"] = {
        "task": "analyzer.refresh_reviewer_assignments",
        "schedule": crontab(
            hour=ANALYZER_REVIEWER_ASSIGNMENT_UTC_HOUR if ANALYZER_REVIEWER_ASSIGNMENT_UTC_HOUR is not None else 0,
            minute=ANALYZER_REVIEWER_ASSIGNMENT_UTC_MINUTE if ANALYZER_REVIEWER_ASSIGNMENT_UTC_MINUTE is not None else 30,
        ),
        "kwargs": {
            "cache_key": "default",
            "fanout": True,
        },
    }
# Apply proposed reviewer assignments to GitHub daily at a fixed UTC clock time (default 00:45),
# i.e. shortly after the compute refresh. PERIOD_SECONDS <= 0 disables scheduling. The task is
# also gated by ANALYZER_REVIEWER_ASSIGNMENT_APPLY_ENABLED, so scheduling it while that flag is
# off is a cheap no-op (it returns feature_disabled).
if ANALYZER_REVIEWER_ASSIGNMENT_APPLY_PERIOD_SECONDS > 0:
    CELERY_BEAT_SCHEDULE["apply_reviewer_assignments"] = {
        "task": "analyzer.apply_reviewer_assignments",
        "schedule": crontab(
            hour=ANALYZER_REVIEWER_ASSIGNMENT_APPLY_UTC_HOUR if ANALYZER_REVIEWER_ASSIGNMENT_APPLY_UTC_HOUR is not None else 0,
            minute=(
                ANALYZER_REVIEWER_ASSIGNMENT_APPLY_UTC_MINUTE if ANALYZER_REVIEWER_ASSIGNMENT_APPLY_UTC_MINUTE is not None else 45
            ),
        ),
    }
# Propose reviewer assignments through the acceptance gate (design doc 050), daily at a fixed UTC
# clock time (default 00:45, just after the compute refresh — same slot as the legacy apply, which
# it supersedes). Beat fires unconditionally; ANALYZER_ASSIGNMENT_PROPOSALS_ENABLED (+ dry-run)
# gates activity inside the task, so scheduling it while off is a cheap no-op (feature_disabled).
if ANALYZER_ASSIGNMENT_PROPOSE_PERIOD_SECONDS > 0:
    CELERY_BEAT_SCHEDULE["propose_reviewer_assignments"] = {
        "task": "analyzer.propose_reviewer_assignments",
        "schedule": crontab(
            hour=ANALYZER_ASSIGNMENT_PROPOSE_UTC_HOUR if ANALYZER_ASSIGNMENT_PROPOSE_UTC_HOUR is not None else 0,
            minute=ANALYZER_ASSIGNMENT_PROPOSE_UTC_MINUTE if ANALYZER_ASSIGNMENT_PROPOSE_UTC_MINUTE is not None else 45,
        ),
    }
# Expiry/reconcile sweep: expire timed-out proposals and supersede those whose PR left the queue.
# Essential maintenance, so it runs on a simple period regardless of the master switch.
if ANALYZER_ASSIGNMENT_PROPOSAL_EXPIRY_PERIOD_SECONDS > 0:
    CELERY_BEAT_SCHEDULE["expire_assignment_proposals"] = {
        "task": "analyzer.expire_assignment_proposals",
        "schedule": ANALYZER_ASSIGNMENT_PROPOSAL_EXPIRY_PERIOD_SECONDS,
    }
# Deliver the per-reviewer proposal digest DM (design doc 050), daily at a fixed UTC clock time
# (default 01:00, just after the propose run). Beat fires unconditionally; the task no-ops unless
# ANALYZER_ASSIGNMENT_PROPOSALS_ENABLED AND _DELIVERY_ENABLED (+ dry-run). PERIOD_SECONDS <= 0
# disables scheduling.
if ANALYZER_ASSIGNMENT_DELIVER_PERIOD_SECONDS > 0:
    CELERY_BEAT_SCHEDULE["deliver_assignment_proposals"] = {
        "task": "analyzer.deliver_assignment_proposals",
        "schedule": crontab(
            hour=ANALYZER_ASSIGNMENT_DELIVER_UTC_HOUR if ANALYZER_ASSIGNMENT_DELIVER_UTC_HOUR is not None else 1,
            minute=ANALYZER_ASSIGNMENT_DELIVER_UTC_MINUTE if ANALYZER_ASSIGNMENT_DELIVER_UTC_MINUTE is not None else 0,
        ),
    }
reviewer_attention_schedule = None
if (ANALYZER_REVIEWER_ATTENTION_UTC_HOUR is not None) or (ANALYZER_REVIEWER_ATTENTION_UTC_MINUTE is not None):
    reviewer_attention_schedule = crontab(
        hour=ANALYZER_REVIEWER_ATTENTION_UTC_HOUR or 0,
        minute=ANALYZER_REVIEWER_ATTENTION_UTC_MINUTE or 0,
    )
elif ANALYZER_REVIEWER_ATTENTION_PERIOD_SECONDS > 0:
    reviewer_attention_schedule = ANALYZER_REVIEWER_ATTENTION_PERIOD_SECONDS

if reviewer_attention_schedule is not None:
    CELERY_BEAT_SCHEDULE["reviewer_attention_daily"] = {
        "task": "analyzer.reviewer_attention_daily",
        "schedule": reviewer_attention_schedule,
    }
CELERY_BEAT_SCHEDULE["reviewer_attention_cleanup"] = {
    "task": "analyzer.reviewer_attention_cleanup",
    "schedule": crontab(
        day_of_week=ANALYZER_REVIEWER_ATTENTION_CLEANUP_DAY_OF_WEEK,
        hour=ANALYZER_REVIEWER_ATTENTION_CLEANUP_UTC_HOUR or 3,
        minute=ANALYZER_REVIEWER_ATTENTION_CLEANUP_UTC_MINUTE or 0,
    ),
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
