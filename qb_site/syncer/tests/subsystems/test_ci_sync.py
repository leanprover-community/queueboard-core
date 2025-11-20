from __future__ import annotations

from django.test import TestCase
from django.utils import timezone

from core.models.repository import Repository
from syncer.models import PullRequest, CheckRun, StatusContext
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
