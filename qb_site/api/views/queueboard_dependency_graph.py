from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
import hashlib
import json
from typing import Any

from django.conf import settings
from django.db import models
from django.db.models.functions import Cast
from django.http import HttpResponse
from django.utils import timezone
from django.utils.http import http_date, parse_http_date_safe
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from analyzer.models import QueueSnapshot
from analyzer.services.dependency_graph import DependencyGraphBuilder
from analyzer.services.queue_rules import default_rule_set_for_repo
from analyzer.tasks.queueboard_snapshot import build_queueboard_snapshot
from api.views.queueboard_snapshot import _as_bool, _etag_matches
from core.models import Repository


class QueueboardDependencyGraphView(APIView):
    """Serve dependency_graph.json derived from cached queueboard snapshots."""

    authentication_classes: list = []
    permission_classes: list = []

    def get(self, request, *args: Any, **kwargs: Any) -> Response:
        repo_param = request.query_params.get("repo")
        if not repo_param:
            return Response({"detail": "repo query parameter is required"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            owner, name = repo_param.split("/", 1)
        except ValueError:
            return Response({"detail": "repo must be formatted as owner/name"}, status=status.HTTP_400_BAD_REQUEST)

        repo = Repository.objects.filter(owner=owner, name=name).first()
        if repo is None:
            return Response({"detail": "repository not found"}, status=status.HTTP_404_NOT_FOUND)

        cache_key_param = request.query_params.get("cache_key")
        if cache_key_param is not None:
            cache_key = cache_key_param
            rule_set_id = None
        else:
            rule_set = default_rule_set_for_repo(repo)
            cache_key = str(rule_set.id) if rule_set else "default"
            rule_set_id = rule_set.id if rule_set else None
        refresh_requested = _as_bool(request.query_params.get("refresh"))
        ttl_seconds = int(getattr(settings, "ANALYZER_QUEUEBOARD_SNAPSHOT_TTL_SECONDS", 0))
        snapshot = (
            QueueSnapshot.objects.filter(repository=repo, cache_key=cache_key)
            .annotate(payload_text=Cast("payload", output_field=models.TextField()))
            .order_by("-generated_at", "-id")
            .first()
        )

        now_ts = timezone.now()
        stale = snapshot is None
        if snapshot:
            if snapshot.expires_at and snapshot.expires_at <= now_ts:
                stale = True
            elif ttl_seconds > 0 and snapshot.generated_at <= now_ts - timedelta(seconds=ttl_seconds):
                stale = True

        refresh_task_id = None
        expires_in = ttl_seconds if ttl_seconds > 0 else None
        if snapshot is None:
            async_res = build_queueboard_snapshot.delay(
                repository_id=repo.id,
                cache_key=cache_key,
                expires_in_seconds=expires_in,
                rule_set_id=rule_set_id,
            )
            refresh_task_id = getattr(async_res, "id", None)
            headers = {}
            if refresh_task_id:
                headers["X-Queueboard-Refresh-Task"] = refresh_task_id
            return Response(
                {"detail": "snapshot not yet available", "refresh_enqueued": True},
                status=status.HTTP_202_ACCEPTED,
                headers=headers,
            )

        if stale or refresh_requested:
            async_res = build_queueboard_snapshot.delay(
                repository_id=repo.id,
                cache_key=cache_key,
                expires_in_seconds=expires_in,
                rule_set_id=rule_set_id,
            )
            refresh_task_id = getattr(async_res, "id", None)

        builder = DependencyGraphBuilder()
        graph = builder.build(repository=repo, snapshot=snapshot.payload or {})
        graph_etag = hashlib.sha256(json.dumps(graph, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        etag_header = f'"{graph_etag}"'

        if not stale and not refresh_requested and _is_not_modified(request, graph_etag, snapshot.generated_at):
            resp = Response(status=status.HTTP_304_NOT_MODIFIED)
            resp["ETag"] = etag_header
            resp["Last-Modified"] = http_date(int(snapshot.generated_at.timestamp()))
            return resp

        headers = {
            "ETag": etag_header,
            "Last-Modified": http_date(int(snapshot.generated_at.timestamp())),
        }
        if refresh_task_id:
            headers["X-Queueboard-Refresh-Task"] = refresh_task_id
        if stale:
            headers["X-Queueboard-Stale"] = "1"

        content = json.dumps(graph)
        return HttpResponse(content, status=status.HTTP_200_OK, headers=headers, content_type="application/json")


def _is_not_modified(request, etag: str, generated_at: datetime) -> bool:
    if _etag_matches(request.META.get("HTTP_IF_NONE_MATCH"), etag):
        return True
    modified_since = request.META.get("HTTP_IF_MODIFIED_SINCE")
    if modified_since:
        parsed = parse_http_date_safe(modified_since)
        if parsed is not None:
            since_dt = datetime.fromtimestamp(parsed, tz=dt_timezone.utc)
            if generated_at <= since_dt:
                return True
    return False
