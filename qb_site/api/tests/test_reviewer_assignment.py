from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from django.utils.http import http_date
from rest_framework.test import APIClient

from analyzer.models import AreaStatsSnapshot, ReviewerAssignmentSnapshot
from core.models import Repository


@override_settings(ANALYZER_REVIEWER_ASSIGNMENT_TTL_SECONDS=300)
class ReviewerAssignmentViewTests(TestCase):
    def setUp(self) -> None:
        self.client = APIClient()
        self.repo = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        self.assignment_payload = {
            "meta": {"schema_version": "v1-draft", "generated_at": "2025-01-01T00:00:00Z", "rule_set_id": "default"},
            "automatic_assignments": {"10": "alice"},
        }
        self.area_payload = {
            "meta": {"schema_version": "v1-draft", "generated_at": "2025-01-01T00:00:00Z", "rule_set_id": "default"},
            "area_stats": {"t-analysis": {"assigned": 1}},
        }

    def _make_assignment_snapshot(self, **overrides) -> ReviewerAssignmentSnapshot:
        now_ts = timezone.now()
        return ReviewerAssignmentSnapshot.objects.create(
            repository=self.repo,
            cache_key=overrides.get("cache_key", "default"),
            generated_at=overrides.get("generated_at", now_ts),
            payload=overrides.get("payload", self.assignment_payload),
            etag=overrides.get("etag", "assign-etag"),
            assignment_count=overrides.get("assignment_count", 1),
            expires_at=overrides.get("expires_at"),
        )

    def _make_area_snapshot(self, **overrides) -> AreaStatsSnapshot:
        now_ts = timezone.now()
        return AreaStatsSnapshot.objects.create(
            repository=self.repo,
            cache_key=overrides.get("cache_key", "default"),
            generated_at=overrides.get("generated_at", now_ts),
            payload=overrides.get("payload", self.area_payload),
            etag=overrides.get("etag", "area-etag"),
            area_count=overrides.get("area_count", 1),
            expires_at=overrides.get("expires_at"),
        )

    def test_assignments_endpoint_returns_payload(self):
        snap = self._make_assignment_snapshot()
        resp = self.client.get("/api/v1/queueboard/automatic-assignments", {"repo": "leanprover-community/mathlib4"})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.json(),
            {"meta": self.assignment_payload["meta"], "automatic_assignments": self.assignment_payload["automatic_assignments"]},
        )
        self.assertEqual(resp["ETag"], f'"{snap.etag}"')
        self.assertEqual(resp["Last-Modified"], http_date(int(snap.generated_at.timestamp())))

    def test_area_stats_endpoint_returns_payload(self):
        self._make_area_snapshot()
        resp = self.client.get("/api/v1/queueboard/area-stats", {"repo": "leanprover-community/mathlib4"})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"meta": self.area_payload["meta"], "area_stats": self.area_payload["area_stats"]})

    def test_conditional_get_uses_etag_and_last_modified(self):
        snap = self._make_assignment_snapshot()
        resp = self.client.get(
            "/api/v1/queueboard/automatic-assignments",
            {"repo": "leanprover-community/mathlib4"},
            HTTP_IF_NONE_MATCH=snap.etag,
            HTTP_IF_MODIFIED_SINCE=http_date(int(snap.generated_at.timestamp())),
        )

        self.assertEqual(resp.status_code, 304)

    def test_enqueues_build_when_snapshot_missing(self):
        with patch("api.views.reviewer_assignment.build_reviewer_assignment.delay") as mock_delay:
            mock_delay.return_value.id = "task123"

            resp = self.client.get("/api/v1/queueboard/automatic-assignments", {"repo": "leanprover-community/mathlib4"})

            self.assertEqual(resp.status_code, 202)
            mock_delay.assert_called_once()
            self.assertEqual(resp.headers.get("X-Queueboard-Refresh-Task"), "task123")

    def test_stale_snapshot_triggers_refresh_but_returns_payload(self):
        stale_time = timezone.now() - timedelta(hours=1)
        snap = self._make_assignment_snapshot(expires_at=stale_time - timedelta(minutes=5), generated_at=stale_time)

        with patch("api.views.reviewer_assignment.build_reviewer_assignment.delay") as mock_delay:
            mock_delay.return_value.id = "task999"

            resp = self.client.get("/api/v1/queueboard/automatic-assignments", {"repo": "leanprover-community/mathlib4"})

            mock_delay.assert_called_once()
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.headers.get("X-Queueboard-Refresh-Task"), "task999")
            self.assertEqual(resp.headers.get("X-Queueboard-Stale"), "1")
            self.assertEqual(
                resp.json(),
                {
                    "meta": self.assignment_payload["meta"],
                    "automatic_assignments": self.assignment_payload["automatic_assignments"],
                },
            )
            self.assertEqual(resp["ETag"], f'"{snap.etag}"')
            self.assertEqual(resp["Last-Modified"], http_date(int(snap.generated_at.timestamp())))

    def test_area_stats_enqueues_build_when_snapshot_missing(self):
        with patch("api.views.reviewer_assignment.build_area_stats.delay") as mock_delay:
            mock_delay.return_value.id = "task-area"

            resp = self.client.get("/api/v1/queueboard/area-stats", {"repo": "leanprover-community/mathlib4"})

            self.assertEqual(resp.status_code, 202)
            mock_delay.assert_called_once()
            self.assertEqual(resp.headers.get("X-Queueboard-Refresh-Task"), "task-area")

    def test_area_stats_stale_snapshot_triggers_refresh(self):
        stale_time = timezone.now() - timedelta(hours=2)
        snap = self._make_area_snapshot(expires_at=stale_time - timedelta(minutes=10), generated_at=stale_time)

        with patch("api.views.reviewer_assignment.build_area_stats.delay") as mock_delay:
            mock_delay.return_value.id = "task-area-2"

            resp = self.client.get("/api/v1/queueboard/area-stats", {"repo": "leanprover-community/mathlib4"})

            mock_delay.assert_called_once()
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.headers.get("X-Queueboard-Refresh-Task"), "task-area-2")
            self.assertEqual(resp.headers.get("X-Queueboard-Stale"), "1")
            self.assertEqual(resp.json(), {"meta": self.area_payload["meta"], "area_stats": self.area_payload["area_stats"]})
            self.assertEqual(resp["ETag"], f'"{snap.etag}"')
            self.assertEqual(resp["Last-Modified"], http_date(int(snap.generated_at.timestamp())))
