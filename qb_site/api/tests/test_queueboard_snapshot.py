from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from django.utils.http import http_date
from rest_framework.test import APIClient

from analyzer.models import QueueSnapshot
from core.models import Repository


@override_settings(ANALYZER_QUEUEBOARD_SNAPSHOT_TTL_SECONDS=300)
class QueueboardSnapshotViewTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.repo = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        self.payload = {
            "meta": {"schema_version": "v1-draft"},
            "prs": {},
            "lists": {"draft_prs": [], "nondraft_prs": [], "dashboards": {}},
        }

    def _make_snapshot(self, **overrides):
        now_ts = timezone.now()
        return QueueSnapshot.objects.create(
            repository=self.repo,
            cache_key=overrides.get("cache_key", "default"),
            generated_at=overrides.get("generated_at", now_ts),
            payload=overrides.get("payload", self.payload),
            etag=overrides.get("etag", "etag123"),
            pr_count=overrides.get("pr_count", 0),
            queue_count=overrides.get("queue_count", 0),
            expires_at=overrides.get("expires_at"),
        )

    def test_returns_snapshot_with_headers(self):
        snap = self._make_snapshot()

        resp = self.client.get("/api/v1/queueboard/snapshot", {"repo": "leanprover-community/mathlib4"})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), self.payload)
        self.assertEqual(resp["ETag"], f'"{snap.etag}"')
        self.assertIn("Last-Modified", resp)

    def test_conditional_get_uses_etag_and_last_modified(self):
        snap = self._make_snapshot()

        resp = self.client.get(
            "/api/v1/queueboard/snapshot",
            {"repo": "leanprover-community/mathlib4"},
            HTTP_IF_NONE_MATCH=snap.etag,
            HTTP_IF_MODIFIED_SINCE=http_date(int(snap.generated_at.timestamp())),
        )

        self.assertEqual(resp.status_code, 304)

    def test_enqueues_build_when_snapshot_missing(self):
        with patch("api.views.queueboard_snapshot.build_queueboard_snapshot.delay") as mock_delay:
            mock_delay.return_value.id = "task123"

            resp = self.client.get("/api/v1/queueboard/snapshot", {"repo": "leanprover-community/mathlib4"})

            self.assertEqual(resp.status_code, 202)
            mock_delay.assert_called_once()
            self.assertEqual(resp.headers.get("X-Queueboard-Refresh-Task"), "task123")

    def test_stale_snapshot_triggers_refresh_but_returns_payload(self):
        stale_time = timezone.now() - timedelta(hours=1)
        self._make_snapshot(expires_at=stale_time - timedelta(minutes=5), generated_at=stale_time)

        with patch("api.views.queueboard_snapshot.build_queueboard_snapshot.delay") as mock_delay:
            mock_delay.return_value.id = "task456"

            resp = self.client.get("/api/v1/queueboard/snapshot", {"repo": "leanprover-community/mathlib4"})

            mock_delay.assert_called_once()
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.headers.get("X-Queueboard-Refresh-Task"), "task456")
            self.assertEqual(resp.headers.get("X-Queueboard-Stale"), "1")
            self.assertEqual(resp.json()["meta"]["schema_version"], "v1-draft")
