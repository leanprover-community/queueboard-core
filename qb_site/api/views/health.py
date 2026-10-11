"""GET /api/health — health check for container health checks and external uptime monitors.

Unlike `/api/`, it touches the app's dependencies: a trivial query on the database and a ping to
the Celery broker's Redis. 200 when every check passes, 503 when any fails. The body names each
check's result and nothing else; failure details go to the log, since the endpoint is public.

A plain Django view rather than DRF: no content negotiation (DRF's browsable API page would set a
CSRF cookie), no authentication, no session access.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from django.db import connection, transaction
from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_safe

from syncer.services.rate_budget import _get_redis_client

log = logging.getLogger(__name__)

# Each check gives up after about this long, so a hung dependency fails the probe instead of
# hanging it. Opening a database connection is not covered: that is bounded by the connection
# settings, not by this view.
CHECK_TIMEOUT_SECONDS = 2


def _check_database() -> None:
    with transaction.atomic(), connection.cursor() as cursor:
        # LOCAL: the timeout ends with this transaction rather than staying on the connection.
        cursor.execute(f"SET LOCAL statement_timeout = {int(CHECK_TIMEOUT_SECONDS * 1000)}")
        cursor.execute("SELECT 1")
        cursor.fetchone()


def _check_broker() -> None:
    client = _get_redis_client(socket_connect_timeout=CHECK_TIMEOUT_SECONDS, socket_timeout=CHECK_TIMEOUT_SECONDS)
    if client is None:
        raise RuntimeError("CELERY_BROKER_URL is not a redis:// or rediss:// URL, or redis-py is missing")
    try:
        client.ping()
    finally:
        client.close()


def _run_check(name: str, check: Callable[[], None]) -> str:
    try:
        check()
    except Exception as exc:
        # One line, no traceback: a monitor probing an outage would otherwise log a traceback per check per probe.
        log.warning("health: %s check failed: %s: %s", name, type(exc).__name__, exc)
        return "error"
    return "ok"


# Exempt because the view takes no unsafe method: without it, the CSRF middleware answers a POST with
# 403 (and logs it) before require_safe can answer 405.
@csrf_exempt
@require_safe
def health(_request: HttpRequest) -> JsonResponse:
    checks = {
        "database": _run_check("database", _check_database),
        "broker": _run_check("broker", _check_broker),
    }
    healthy = all(result == "ok" for result in checks.values())
    response = JsonResponse({"status": "ok" if healthy else "error", "checks": checks}, status=200 if healthy else 503)
    response["Cache-Control"] = "no-store"
    return response
