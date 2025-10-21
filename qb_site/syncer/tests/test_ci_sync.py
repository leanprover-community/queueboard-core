from __future__ import annotations

from django.test import TestCase
from django.utils import timezone

from core.models.repository import Repository
from syncer.models import PullRequest, CheckRun, StatusContext
from syncer.services.sub.ci_sync import sync_check_runs, sync_status_contexts


class TestCISync(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="o", name="r", default_branch="master", is_active=True)
        self.pr = PullRequest.objects.create(
            repository=self.repo,
            number=1,
            state="open",
            is_draft=False,
            gh_created_at=timezone.now(),
            gh_updated_at=timezone.now(),
            base_ref_name="master",
            head_ref_name="b",
            head_repo_owner_login="o",
            head_repo_name="fork",
            title="t",
            body="",
            additions=0,
            deletions=0,
            changed_files_count=0,
        )

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
        res = sync_check_runs(self.pr, ctxs, head_sha)
        self.assertEqual(CheckRun.objects.filter(pull_request=self.pr).count(), 1)
        self.assertEqual(res.created, 1)
        # Update
        ctxs[0]["status"] = "COMPLETED"
        ctxs[0]["conclusion"] = "SUCCESS"
        ctxs[0]["completedAt"] = "2025-10-20T01:00:00Z"
        res2 = sync_check_runs(self.pr, ctxs, head_sha)
        self.assertEqual(res2.updated, 1)

    def test_status_context_upsert(self) -> None:
        head_sha = "abc1234"
        ctxs = [
            {
                "id": "SC1",
                "context": "bors",
                "state": "SUCCESS",
                "targetUrl": None,
                "description": "",
                "createdAt": "2025-10-20T00:00:00Z"
            }
        ]
        res = sync_status_contexts(self.pr, ctxs, head_sha)
        self.assertEqual(StatusContext.objects.filter(pull_request=self.pr).count(), 1)
        self.assertEqual(res.created, 1)
        # Update state
        ctxs[0]["state"] = "PENDING"
        res2 = sync_status_contexts(self.pr, ctxs, head_sha)
        self.assertEqual(res2.updated, 1)
