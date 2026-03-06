from __future__ import annotations

import hashlib
import hmac
import logging
import json
from urllib.parse import parse_qs

from django.conf import settings
from django.db import IntegrityError
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from core.models import Repository
from syncer.models import GitHubWebhookDelivery, GitHubWebhookDeliveryStatus
from syncer.services.github_webhook_router import route_github_webhook
from syncer.tasks.sync_tasks import sync_pr_task

logger = logging.getLogger(__name__)


def _has_valid_github_signature(*, payload: bytes, signature_header: str, secret: str) -> bool:
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    given_sig = signature_header.removeprefix("sha256=").strip()
    if not given_sig:
        return False
    expected_sig = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(given_sig, expected_sig)


def _parse_webhook_payload(payload: bytes) -> dict:
    """Parse GitHub webhook body for both JSON and form-encoded webhook modes."""
    try:
        parsed = json.loads(payload or b"{}")
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # GitHub can send form-urlencoded payloads with JSON in `payload=...`.
    try:
        qs = parse_qs((payload or b"").decode("utf-8"), keep_blank_values=True)
    except UnicodeDecodeError:
        return {}
    payload_entries = qs.get("payload") or []
    if not payload_entries:
        return {}
    raw = payload_entries[0]
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        parsed_form = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed_form if isinstance(parsed_form, dict) else {}


def _enqueue_pull_request_sync(summary: dict) -> dict:
    """Enqueue syncer.sync_pr for pull_request webhook events when routable."""
    route = str(summary.get("route") or "")
    if route != "pull_request":
        return summary

    repo_meta = summary.get("repository") if isinstance(summary.get("repository"), dict) else {}
    owner = str(repo_meta.get("owner") or "") if isinstance(repo_meta, dict) else ""
    name = str(repo_meta.get("name") or "") if isinstance(repo_meta, dict) else ""
    pr_numbers_raw = summary.get("pr_numbers") if isinstance(summary.get("pr_numbers"), list) else []
    pr_numbers = [n for n in pr_numbers_raw if isinstance(n, int)]

    if not owner or not name:
        summary["reason"] = "missing_repository"
        summary["enqueued_sync_prs"] = 0
        summary["sync_pr_task_ids"] = []
        return summary
    if not pr_numbers:
        summary["reason"] = "missing_pr_number"
        summary["enqueued_sync_prs"] = 0
        summary["sync_pr_task_ids"] = []
        return summary

    repo = Repository.objects.filter(owner=owner, name=name, is_active=True).only("id").first()
    if repo is None:
        summary["reason"] = "repository_not_active_or_missing"
        summary["enqueued_sync_prs"] = 0
        summary["sync_pr_task_ids"] = []
        return summary

    task_ids: list[str] = []
    for pr_number in sorted(set(pr_numbers)):
        async_res = sync_pr_task.delay(repo.id, pr_number)
        task_ids.append(str(async_res.id))
    summary["reason"] = "enqueued_sync_pr"
    summary["enqueued_sync_prs"] = len(task_ids)
    summary["sync_pr_task_ids"] = task_ids
    return summary


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
    if not delivery:
        return JsonResponse({"error": "Missing delivery id"}, status=400)

    payload_data = _parse_webhook_payload(request.body)

    summary = route_github_webhook(event=event, payload=payload_data)
    summary = _enqueue_pull_request_sync(summary)
    repo_meta = summary.get("repository") if isinstance(summary.get("repository"), dict) else {}
    repo_owner = str(repo_meta.get("owner") or "") if isinstance(repo_meta, dict) else ""
    repo_name = str(repo_meta.get("name") or "") if isinstance(repo_meta, dict) else ""
    action = str(summary.get("action") or "")

    try:
        GitHubWebhookDelivery.objects.create(
            delivery_id=delivery,
            event_type=event,
            action=action,
            repository_owner=repo_owner,
            repository_name=repo_name,
            processed_at=timezone.now(),
            status=GitHubWebhookDeliveryStatus.ACCEPTED,
            summary_json=summary,
        )
    except IntegrityError:
        logger.info("github_webhook_duplicate event=%s delivery=%s", event, delivery)
        return JsonResponse({"status": "duplicate"}, status=202)

    logger.info(
        "github_webhook_accepted event=%s delivery=%s route=%s payload_bytes=%s",
        event,
        delivery,
        summary.get("route"),
        len(request.body),
    )
    return JsonResponse({"status": "accepted"}, status=202)
