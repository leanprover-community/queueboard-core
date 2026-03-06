from __future__ import annotations

import hashlib
import hmac
import logging

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

logger = logging.getLogger(__name__)


def _has_valid_github_signature(*, payload: bytes, signature_header: str, secret: str) -> bool:
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    given_sig = signature_header.removeprefix("sha256=").strip()
    if not given_sig:
        return False
    expected_sig = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(given_sig, expected_sig)


@csrf_exempt
def github_webhook(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    if not bool(getattr(settings, "SYNCER_GITHUB_WEBHOOK_ENABLED", False)):
        return JsonResponse({"error": "Not found"}, status=404)

    secret = str(getattr(settings, "GITHUB_WEBHOOK_SECRET", "") or "")
    if not secret:
        logger.error("github_webhook_misconfigured: missing GITHUB_WEBHOOK_SECRET")
        return JsonResponse({"error": "Webhook misconfigured"}, status=503)

    signature = request.headers.get("X-Hub-Signature-256", "")
    if not _has_valid_github_signature(payload=request.body, signature_header=signature, secret=secret):
        return JsonResponse({"error": "Forbidden"}, status=403)

    event = request.headers.get("X-GitHub-Event", "")
    delivery = request.headers.get("X-GitHub-Delivery", "")
    logger.info(
        "github_webhook_accepted event=%s delivery=%s payload_bytes=%s",
        event,
        delivery,
        len(request.body),
    )
    return JsonResponse({"status": "accepted"}, status=202)
