"""POST /api/v1/analytics/collect — lightweight pageview ingestion endpoint."""

from __future__ import annotations

from django.conf import settings
from django.utils import timezone
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from site_analytics.models import AnalyticsPageView
from site_analytics.services.bot_filter import is_bot
from site_analytics.services.hashing import compute_visitor_hash, get_client_ip

# Hard caps to guard against oversized payloads hitting DB column limits.
_PATH_MAX = 2000
_REFERRER_MAX = 2000
_UA_MAX = 1000

# CORS headers added to every response so browsers on third-party/static sites
# can call this endpoint without a server-side proxy.
_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
}


def _cors(response: Response) -> Response:
    for key, value in _CORS_HEADERS.items():
        response[key] = value
    return response


class AnalyticsCollectView(APIView):
    """Ingest a single pageview event.

    Intentionally minimal: validate, hash, insert, return 204.
    All heavier work (aggregation, reporting) happens in periodic tasks.

    CORS headers are always returned so browsers on third-party static sites
    can call this endpoint directly.
    """

    authentication_classes: list = []
    permission_classes: list = []

    def options(self, request: Request, *args: object, **kwargs: object) -> Response:
        """Handle CORS preflight requests."""
        return _cors(Response(status=status.HTTP_204_NO_CONTENT))

    def post(self, request: Request, *args: object, **kwargs: object) -> Response:
        site = (request.data.get("site") or "").strip()
        path = (request.data.get("path") or "").strip()
        referrer = (request.data.get("referrer") or "").strip()
        user_agent = request.META.get("HTTP_USER_AGENT", "").strip()

        if not site:
            return _cors(Response({"detail": "site is required"}, status=status.HTTP_400_BAD_REQUEST))
        if not path:
            return _cors(Response({"detail": "path is required"}, status=status.HTTP_400_BAD_REQUEST))

        allowed_sites = settings.SITE_ANALYTICS_ALLOWED_SITES
        if site not in allowed_sites:
            return _cors(Response({"detail": "unknown site"}, status=status.HTTP_400_BAD_REQUEST))

        # Reject empty UA when the stricter hardening flag is enabled.
        if not user_agent and settings.SITE_ANALYTICS_REJECT_EMPTY_UA:
            return _cors(Response(status=status.HTTP_204_NO_CONTENT))

        # Silently drop bot traffic rather than returning an error, to avoid
        # leaking information about detection heuristics.
        if is_bot(user_agent):
            return _cors(Response(status=status.HTTP_204_NO_CONTENT))

        now = timezone.now()
        visitor_month_hash = compute_visitor_hash(get_client_ip(request), user_agent)

        AnalyticsPageView.objects.create(
            site=site,
            path=path[:_PATH_MAX],
            referrer=referrer[:_REFERRER_MAX],
            user_agent=user_agent[:_UA_MAX],
            occurred_at=now,
            visitor_month_hash=visitor_month_hash,
        )

        return _cors(Response(status=status.HTTP_204_NO_CONTENT))
