from __future__ import annotations

from datetime import timedelta
import hashlib
import json
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from django.utils.http import http_date
from rest_framework.test import APIClient

from analyzer.models import QueueSnapshot
from analyzer.services.dependency_graph import DependencyGraphBuilder
from core.models import Repository


@override_settings(ANALYZER_QUEUEBOARD_SNAPSHOT_TTL_SECONDS=300)
class QueueboardDependencyGraphViewTests(TestCase):
    def setUp(self) -> None:
        self.client = APIClient()
        self.repo = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        self.snapshot_payload = {
            "meta": {"schema_version": "v1-draft"},
            "prs": {
                1: {
                    "state": "open",
                    "is_draft": False,
                    "title": "PR 1",
                    "author": "alice",
                    "labels": [{"name": "t-alpha", "color": "123456"}],
                    "direct_dependencies": [2, 99],
                    "additions": 1,
                    "deletions": 1,
                },
                2: {
                    "state": "open",
                    "is_draft": False,
                    "title": "PR 2",
                    "author": "bob",
                    "labels": [{"name": "wip", "color": "ffffff"}],
                    "direct_dependencies": [],
                    "additions": 2,
                    "deletions": 0,
                },
            },
            "lists": {"draft_prs": [], "nondraft_prs": [1, 2], "dashboards": {}},
        }

    def _make_snapshot(self, **overrides) -> QueueSnapshot:
        now_ts = timezone.now()
        return QueueSnapshot.objects.create(
            repository=self.repo,
            cache_key=overrides.get("cache_key", "default"),
            generated_at=overrides.get("generated_at", now_ts),
            payload=overrides.get("payload", self.snapshot_payload),
            etag=overrides.get("etag", "snap-etag"),
            pr_count=overrides.get("pr_count", 2),
            queue_count=overrides.get("queue_count", 1),
            expires_at=overrides.get("expires_at"),
        )

    def _expected_graph(self, payload: dict | None = None) -> dict:
        builder = DependencyGraphBuilder()
        return builder.build(repository=self.repo, snapshot=payload or self.snapshot_payload)

    def _graph_etag(self, graph: dict) -> str:
        return hashlib.sha256(json.dumps(graph, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    def test_returns_graph_with_headers(self):
        snap = self._make_snapshot()
        expected = self._expected_graph()
        resp = self.client.get("/api/v1/queueboard/dependency-graph", {"repo": "leanprover-community/mathlib4"})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), expected)
        self.assertEqual(resp["ETag"], f'"{self._graph_etag(expected)}"')
        self.assertEqual(resp["Last-Modified"], http_date(int(snap.generated_at.timestamp())))

    def test_conditional_get_uses_etag_and_last_modified(self):
        snap = self._make_snapshot()
        expected = self._expected_graph()
        etag = self._graph_etag(expected)

        resp = self.client.get(
            "/api/v1/queueboard/dependency-graph",
            {"repo": "leanprover-community/mathlib4"},
            HTTP_IF_NONE_MATCH=etag,
            HTTP_IF_MODIFIED_SINCE=http_date(int(snap.generated_at.timestamp())),
        )

        self.assertEqual(resp.status_code, 304)

    def test_enqueues_build_when_snapshot_missing(self):
        with patch("api.views.queueboard_dependency_graph.build_queueboard_snapshot.delay") as mock_delay:
            mock_delay.return_value.id = "task789"

            resp = self.client.get("/api/v1/queueboard/dependency-graph", {"repo": "leanprover-community/mathlib4"})

            self.assertEqual(resp.status_code, 202)
            mock_delay.assert_called_once()
            self.assertEqual(resp.headers.get("X-Queueboard-Refresh-Task"), "task789")

    def test_stale_snapshot_triggers_refresh_but_returns_payload(self):
        stale_time = timezone.now() - timedelta(hours=1)
        snap = self._make_snapshot(expires_at=stale_time - timedelta(minutes=5), generated_at=stale_time)
        expected = self._expected_graph()

        with patch("api.views.queueboard_dependency_graph.build_queueboard_snapshot.delay") as mock_delay:
            mock_delay.return_value.id = "task999"

            resp = self.client.get("/api/v1/queueboard/dependency-graph", {"repo": "leanprover-community/mathlib4"})

            mock_delay.assert_called_once()
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.headers.get("X-Queueboard-Refresh-Task"), "task999")
            self.assertEqual(resp.headers.get("X-Queueboard-Stale"), "1")
            self.assertEqual(resp.json(), expected)
            self.assertEqual(resp["ETag"], f'"{self._graph_etag(expected)}"')
            self.assertEqual(resp["Last-Modified"], http_date(int(snap.generated_at.timestamp())))
