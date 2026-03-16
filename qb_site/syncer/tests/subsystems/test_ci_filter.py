from __future__ import annotations

from django.test import TestCase, override_settings

from syncer.tests.factories import make_repo, make_pr
from syncer.models import CommitCheckRun, CommitStatusContext
from syncer.services.sub.ci_sync import sync_check_runs, sync_status_contexts


class TestCIFilter(TestCase):
    def setUp(self) -> None:
        self.repo = make_repo()
        self.pr = make_pr(self.repo, 42)
        self.sha = "abc123"

    @override_settings(SYNCER_CI_FILTER_MODE="allowlist", SYNCER_CI_ALLOW_CHECKRUN_NAMES="build, test")
    def test_checkrun_allow_filters_by_substring(self) -> None:
        ctxs = [
            {
                "id": "CR_A",
                "name": "build (ubuntu)",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "startedAt": None,
                "completedAt": None,
                "detailsUrl": None,
                "externalId": None,
            },
            {
                "id": "CR_B",
                "name": "lint",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "startedAt": None,
                "completedAt": None,
                "detailsUrl": None,
                "externalId": None,
            },
            {
                "id": "CR_C",
                "name": "test: py3.12",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "startedAt": None,
                "completedAt": None,
                "detailsUrl": None,
                "externalId": None,
            },
        ]
        res = sync_check_runs(self.pr, ctxs, self.sha)
        # Only build and test are kept; lint is dropped
        self.assertEqual(CommitCheckRun.objects.filter(repository=self.repo, head_sha=self.sha).count(), 2)
        self.assertEqual(res.created, 2)

    @override_settings(SYNCER_CI_FILTER_MODE="allowlist", SYNCER_CI_ALLOW_STATUS_NAMES="bors, required")
    def test_status_allow_filters_by_substring(self) -> None:
        ctxs = [
            {
                "id": "SC_1",
                "context": "bors",
                "state": "SUCCESS",
                "targetUrl": None,
                "description": None,
                "createdAt": "2025-10-20T00:00:00Z",
            },
            {
                "id": "SC_2",
                "context": "ci/circleci: build",
                "state": "SUCCESS",
                "targetUrl": None,
                "description": None,
                "createdAt": "2025-10-20T00:05:00Z",
            },
            {
                "id": "SC_3",
                "context": "required-checks",
                "state": "SUCCESS",
                "targetUrl": None,
                "description": None,
                "createdAt": "2025-10-20T00:10:00Z",
            },
        ]
        res = sync_status_contexts(self.pr, ctxs, self.sha)
        # Keep bors and required-checks; drop circleci build
        self.assertEqual(CommitStatusContext.objects.filter(repository=self.repo, head_sha=self.sha).count(), 2)
        self.assertEqual(res.created, 2)
