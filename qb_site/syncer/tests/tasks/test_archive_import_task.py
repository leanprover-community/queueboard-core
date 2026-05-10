"""Tests for the archive importer per-item Celery task (design doc 043 Commit 3)."""

from __future__ import annotations

import json
from unittest import mock

import requests
from django.test import TestCase, override_settings

from syncer.models import ArchiveImportItem, ArchiveImportItemStatus, PullRequest
from syncer.tasks.archive_import import import_archive_pr_item
from syncer.tests.factories import make_repo


def _minimal_pr_info_bytes(number: int = 200) -> bytes:
    payload = {
        "number": number,
        "state": "OPEN",
        "isDraft": False,
        "title": "T",
        "body": "",
        "createdAt": "2024-01-01T00:00:00Z",
        "updatedAt": "2024-01-01T00:00:00Z",
        "baseRefName": "master",
        "headRefName": "topic",
        "headRefOid": "head-sha",
        "headRepositoryOwner": {"login": "o"},
        "headRepository": {"name": "fork"},
        "additions": 0,
        "deletions": 0,
        "changedFiles": 0,
        "author": {"login": "alice"},
        "labels": {"nodes": []},
        "timelineItems": {"nodes": []},
        "commits": {"nodes": []},
    }
    return json.dumps(payload).encode("utf-8")


def _make_response(*, status_code: int = 200, content: bytes = b"") -> mock.Mock:
    resp = mock.Mock(spec=requests.Response)
    resp.status_code = status_code
    resp.content = content
    if 400 <= status_code:
        resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
    else:
        resp.raise_for_status.return_value = None
    return resp


class TestImportArchivePRItemTask(TestCase):
    def setUp(self) -> None:
        self.repo = make_repo(owner="leanprover-community", name="mathlib4")
        self.item = ArchiveImportItem.objects.create(
            repository=self.repo,
            archive_name="queueboard-archive2",
            pr_number=200,
            archive_path="data/200/pr_info.json",
        )

    @mock.patch("syncer.services.archive_import.requests.get")
    def test_success_marks_completed_and_creates_pr(self, mock_get) -> None:
        mock_get.return_value = _make_response(content=_minimal_pr_info_bytes(200))
        res = import_archive_pr_item(self.item.pk)
        self.assertEqual(res["status"], ArchiveImportItemStatus.COMPLETED.value)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, ArchiveImportItemStatus.COMPLETED)
        self.assertIsNotNone(self.item.completed_at)
        self.assertEqual(self.item.last_error, "")
        self.assertTrue(PullRequest.objects.filter(repository=self.repo, number=200).exists())

    @mock.patch("syncer.services.archive_import.requests.get")
    def test_404_marks_failed_permanent(self, mock_get) -> None:
        mock_get.return_value = _make_response(status_code=404)
        res = import_archive_pr_item(self.item.pk)
        self.assertEqual(res["status"], ArchiveImportItemStatus.FAILED_PERMANENT.value)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, ArchiveImportItemStatus.FAILED_PERMANENT)
        self.assertIn("http_404", self.item.last_error)

    @mock.patch("syncer.services.archive_import.requests.get")
    def test_5xx_marks_transient_and_increments_attempts(self, mock_get) -> None:
        mock_get.return_value = _make_response(status_code=500)
        res = import_archive_pr_item(self.item.pk)
        self.assertEqual(res["status"], ArchiveImportItemStatus.FAILED_TRANSIENT.value)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, ArchiveImportItemStatus.FAILED_TRANSIENT)
        self.assertEqual(self.item.attempts, 1)

    @override_settings(ARCHIVE_IMPORT_MAX_TRANSIENT_ATTEMPTS=2)
    @mock.patch("syncer.services.archive_import.requests.get")
    def test_transient_attempts_cap_flips_to_permanent(self, mock_get) -> None:
        mock_get.return_value = _make_response(status_code=500)
        # First attempt → transient.
        import_archive_pr_item(self.item.pk)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, ArchiveImportItemStatus.FAILED_TRANSIENT)
        # Second attempt hits the cap (>= 2) and flips to permanent.
        import_archive_pr_item(self.item.pk)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, ArchiveImportItemStatus.FAILED_PERMANENT)
        self.assertIn("ARCHIVE_IMPORT_MAX_TRANSIENT_ATTEMPTS", self.item.last_error)

    @mock.patch("syncer.services.archive_import.requests.get")
    def test_network_error_marks_transient(self, mock_get) -> None:
        mock_get.side_effect = requests.ConnectionError("boom")
        res = import_archive_pr_item(self.item.pk)
        self.assertEqual(res["status"], ArchiveImportItemStatus.FAILED_TRANSIENT.value)

    @mock.patch("syncer.services.archive_import.requests.get")
    def test_invalid_json_marks_permanent(self, mock_get) -> None:
        mock_get.return_value = _make_response(content=b"<html>not json")
        res = import_archive_pr_item(self.item.pk)
        self.assertEqual(res["status"], ArchiveImportItemStatus.FAILED_PERMANENT.value)
        self.item.refresh_from_db()
        self.assertIn("json_decode_error", self.item.last_error)

    @mock.patch("syncer.services.archive_import.requests.get")
    def test_unrecognizable_payload_shape_marks_permanent(self, mock_get) -> None:
        mock_get.return_value = _make_response(content=b'{"not": "a pr"}')
        res = import_archive_pr_item(self.item.pk)
        self.assertEqual(res["status"], ArchiveImportItemStatus.FAILED_PERMANENT.value)
        self.item.refresh_from_db()
        self.assertIn("payload_shape_error", self.item.last_error)

    @mock.patch("syncer.services.archive_import.requests.get")
    def test_double_pickup_short_circuits(self, mock_get) -> None:
        # First run claims the row.
        mock_get.return_value = _make_response(content=_minimal_pr_info_bytes(200))
        import_archive_pr_item(self.item.pk)
        # Second run on the now-completed item finds no claimable row.
        res = import_archive_pr_item(self.item.pk)
        self.assertEqual(res.get("status"), "skipped")

    @mock.patch("syncer.services.archive_import.requests.get")
    def test_failed_transient_can_be_re_picked(self, mock_get) -> None:
        # First run sets transient.
        mock_get.return_value = _make_response(status_code=500)
        import_archive_pr_item(self.item.pk)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, ArchiveImportItemStatus.FAILED_TRANSIENT)
        # Second run with a successful response transitions to completed.
        mock_get.return_value = _make_response(content=_minimal_pr_info_bytes(200))
        res = import_archive_pr_item(self.item.pk)
        self.assertEqual(res["status"], ArchiveImportItemStatus.COMPLETED.value)
