from __future__ import annotations

from django.test import TestCase
from django.utils import timezone

from syncer.models import CheckRun, StatusContext
from syncer.services.sub.ci_sync import sync_check_runs, sync_status_contexts
from syncer.tests.factories import make_repo, make_pr
from analyzer.models import PRRevisionBuildState


class TestCISync(TestCase):
    def setUp(self) -> None:
        self.repo = make_repo()
        self.pr = make_pr(self.repo, 1)

    def test_checkrun_upsert(self) -> None:
        head_sha = "abc1234"
        ctxs = [
            {
                "id": "CR1",
                "name": "build",
                "status": "IN_PROGRESS",
                "conclusion": None,
                "startedAt": "2025-10-20T00:00:00Z",
                "completedAt": None,
                "detailsUrl": None,
                "externalId": None,
            }
        ]
        before = timezone.now()
        res = sync_check_runs(self.pr, ctxs, head_sha)
        self.assertEqual(CheckRun.objects.filter(pull_request=self.pr).count(), 1)
        self.assertEqual(res.created, 1)
        cr = CheckRun.objects.get(pull_request=self.pr)
        self.assertIsNotNone(cr.last_synced_at)
        self.assertGreaterEqual(cr.last_synced_at, before)
        # Update
        ctxs[0]["status"] = "COMPLETED"
        ctxs[0]["conclusion"] = "SUCCESS"
        ctxs[0]["completedAt"] = "2025-10-20T01:00:00Z"
        before2 = timezone.now()
        res2 = sync_check_runs(self.pr, ctxs, head_sha)
        self.assertEqual(res2.updated, 1)
        cr.refresh_from_db()
        self.assertIsNotNone(cr.last_synced_at)
        self.assertGreaterEqual(cr.last_synced_at, before2)

    def test_status_context_upsert(self) -> None:
        head_sha = "abc1234"
        ctxs = [
            {
                "id": "SC1",
                "context": "bors",
                "state": "SUCCESS",
                "targetUrl": None,
                "description": "",
                "createdAt": "2025-10-20T00:00:00Z",
            }
        ]
        before = timezone.now()
        res = sync_status_contexts(self.pr, ctxs, head_sha)
        self.assertEqual(StatusContext.objects.filter(pull_request=self.pr).count(), 1)
        self.assertEqual(res.created, 1)
        sc = StatusContext.objects.get(pull_request=self.pr)
        self.assertIsNotNone(sc.last_synced_at)
        self.assertGreaterEqual(sc.last_synced_at, before)
        # Update state
        ctxs[0]["state"] = "PENDING"
        before2 = timezone.now()
        res2 = sync_status_contexts(self.pr, ctxs, head_sha)
        self.assertEqual(res2.updated, 1)
        sc.refresh_from_db()
        self.assertIsNotNone(sc.last_synced_at)
        self.assertGreaterEqual(sc.last_synced_at, before2)

    def test_marks_revision_dirty_for_earlier_checkrun(self) -> None:
        built_through = timezone.now()
        PRRevisionBuildState.objects.create(
            pull_request=self.pr,
            built_through_ts=built_through,
            dirty_from_ts=None,
        )
        completed = (built_through - timezone.timedelta(hours=3)).replace(microsecond=0)
        started = completed - timezone.timedelta(minutes=10)
        head_sha = "abc1234"
        ctxs = [
            {
                "id": "CR_LATE",
                "name": "build",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "startedAt": started.isoformat(),
                "completedAt": completed.isoformat(),
                "detailsUrl": None,
                "externalId": None,
            }
        ]
        sync_check_runs(self.pr, ctxs, head_sha)
        state = PRRevisionBuildState.objects.get(pull_request=self.pr)
        self.assertEqual(state.dirty_from_ts, started)

    def test_marks_revision_dirty_for_earlier_status(self) -> None:
        built_through = timezone.now()
        PRRevisionBuildState.objects.create(
            pull_request=self.pr,
            built_through_ts=built_through,
            dirty_from_ts=None,
        )
        earlier = (built_through - timezone.timedelta(hours=1)).replace(microsecond=0)
        head_sha = "abc1234"
        ctxs = [
            {
                "id": "SC_LATE",
                "context": "bors",
                "state": "SUCCESS",
                "targetUrl": None,
                "description": "",
                "createdAt": earlier.isoformat(),
            }
        ]
        sync_status_contexts(self.pr, ctxs, head_sha)
        state = PRRevisionBuildState.objects.get(pull_request=self.pr)
        self.assertEqual(state.dirty_from_ts, earlier)

    def test_no_dirty_mark_when_checkrun_snapshot_is_unchanged(self) -> None:
        head_sha = "abc1234"
        started = timezone.now() - timezone.timedelta(hours=2)
        completed = started + timezone.timedelta(minutes=10)
        ctxs = [
            {
                "id": "CR_STABLE",
                "name": "build",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "startedAt": started.isoformat(),
                "completedAt": completed.isoformat(),
                "detailsUrl": None,
                "externalId": None,
            }
        ]
        sync_check_runs(self.pr, ctxs, head_sha)
        PRRevisionBuildState.objects.update_or_create(
            pull_request=self.pr,
            defaults={
                "built_through_ts": timezone.now(),
                "dirty_from_ts": None,
            },
        )

        sync_check_runs(self.pr, ctxs, head_sha)
        state = PRRevisionBuildState.objects.get(pull_request=self.pr)
        self.assertIsNone(state.dirty_from_ts)

    def test_no_dirty_mark_when_status_snapshot_is_unchanged(self) -> None:
        head_sha = "abc1234"
        created_at = timezone.now() - timezone.timedelta(hours=2)
        ctxs = [
            {
                "id": "SC_STABLE",
                "context": "bors",
                "state": "SUCCESS",
                "targetUrl": None,
                "description": "",
                "createdAt": created_at.isoformat(),
            }
        ]
        sync_status_contexts(self.pr, ctxs, head_sha)
        PRRevisionBuildState.objects.update_or_create(
            pull_request=self.pr,
            defaults={
                "built_through_ts": timezone.now(),
                "dirty_from_ts": None,
            },
        )

        sync_status_contexts(self.pr, ctxs, head_sha)
        state = PRRevisionBuildState.objects.get(pull_request=self.pr)
        self.assertIsNone(state.dirty_from_ts)

    def test_prunes_older_snapshot_status_contexts(self) -> None:
        head_sha = "abc1234"
        ctxs_old = [
            {
                "id": "SC_OLD",
                "context": "bors",
                "state": "PENDING",
                "targetUrl": None,
                "description": "",
                "createdAt": "2025-10-20T00:00:00Z",
            }
        ]
        ctxs_new = [
            {
                "id": "SC_NEW",
                "context": "bors",
                "state": "SUCCESS",
                "targetUrl": None,
                "description": "",
                "createdAt": "2025-10-20T01:00:00Z",
            }
        ]
        sync_status_contexts(self.pr, ctxs_old, head_sha)
        self.assertEqual(
            StatusContext.objects.filter(pull_request=self.pr, head_sha=head_sha, name="bors").count(),
            1,
        )
        sync_status_contexts(self.pr, ctxs_new, head_sha)
        rows = StatusContext.objects.filter(pull_request=self.pr, head_sha=head_sha, name="bors")
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().github_node_id, "SC_NEW")

    def test_prunes_older_snapshot_check_runs(self) -> None:
        head_sha = "abc1234"
        ctxs_old = [
            {
                "id": "CR_OLD",
                "name": "build",
                "status": "IN_PROGRESS",
                "conclusion": None,
                "startedAt": "2025-10-20T00:00:00Z",
                "completedAt": None,
                "detailsUrl": None,
                "externalId": None,
            }
        ]
        ctxs_new = [
            {
                "id": "CR_NEW",
                "name": "build",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "startedAt": "2025-10-20T00:10:00Z",
                "completedAt": "2025-10-20T00:20:00Z",
                "detailsUrl": None,
                "externalId": None,
            }
        ]
        sync_check_runs(self.pr, ctxs_old, head_sha)
        self.assertEqual(
            CheckRun.objects.filter(pull_request=self.pr, head_sha=head_sha, name="build").count(),
            1,
        )
        sync_check_runs(self.pr, ctxs_new, head_sha)
        rows = CheckRun.objects.filter(pull_request=self.pr, head_sha=head_sha, name="build")
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().github_node_id, "CR_NEW")
