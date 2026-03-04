from __future__ import annotations

from django.test import TestCase, override_settings
from django.utils import timezone

from syncer.models import CheckRun, CommitCheckRun, CommitStatusContext, StatusContext
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

    def test_checkrun_old_snapshot_in_payload_does_not_dirty(self) -> None:
        head_sha = "abc1234"
        built_through = timezone.now()
        old_started = built_through - timezone.timedelta(days=2)
        old_completed = old_started + timezone.timedelta(minutes=10)
        new_started = built_through + timezone.timedelta(minutes=5)
        new_completed = new_started + timezone.timedelta(minutes=10)
        PRRevisionBuildState.objects.update_or_create(
            pull_request=self.pr,
            defaults={
                "built_through_ts": built_through,
                "dirty_from_ts": None,
            },
        )
        ctxs = [
            {
                "id": "CR_OLD",
                "name": "build",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "startedAt": old_started.isoformat(),
                "completedAt": old_completed.isoformat(),
                "detailsUrl": None,
                "externalId": None,
            },
            {
                "id": "CR_NEW",
                "name": "build",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "startedAt": new_started.isoformat(),
                "completedAt": new_completed.isoformat(),
                "detailsUrl": None,
                "externalId": None,
            },
        ]

        sync_check_runs(self.pr, ctxs, head_sha)
        state = PRRevisionBuildState.objects.get(pull_request=self.pr)
        self.assertIsNone(state.dirty_from_ts)

    def test_status_old_snapshot_in_payload_does_not_dirty(self) -> None:
        head_sha = "abc1234"
        built_through = timezone.now()
        old_created = built_through - timezone.timedelta(days=2)
        new_created = built_through + timezone.timedelta(minutes=5)
        PRRevisionBuildState.objects.update_or_create(
            pull_request=self.pr,
            defaults={
                "built_through_ts": built_through,
                "dirty_from_ts": None,
            },
        )
        ctxs = [
            {
                "id": "SC_OLD",
                "context": "bors",
                "state": "SUCCESS",
                "targetUrl": None,
                "description": "",
                "createdAt": old_created.isoformat(),
            },
            {
                "id": "SC_NEW",
                "context": "bors",
                "state": "SUCCESS",
                "targetUrl": None,
                "description": "",
                "createdAt": new_created.isoformat(),
            },
        ]

        sync_status_contexts(self.pr, ctxs, head_sha)
        state = PRRevisionBuildState.objects.get(pull_request=self.pr)
        self.assertIsNone(state.dirty_from_ts)

    def test_checkrun_mixed_names_dirty_from_latest_per_name_only(self) -> None:
        head_sha = "abc1234"
        built_through = timezone.now().replace(microsecond=0)
        build_old_started = built_through - timezone.timedelta(days=2)
        build_old_completed = build_old_started + timezone.timedelta(minutes=10)
        build_new_started = built_through + timezone.timedelta(minutes=30)
        build_new_completed = build_new_started + timezone.timedelta(minutes=10)
        lint_started = built_through - timezone.timedelta(hours=2)
        lint_completed = lint_started + timezone.timedelta(minutes=5)
        PRRevisionBuildState.objects.update_or_create(
            pull_request=self.pr,
            defaults={
                "built_through_ts": built_through,
                "dirty_from_ts": None,
            },
        )
        ctxs = [
            {
                "id": "CR_BUILD_OLD",
                "name": "build",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "startedAt": build_old_started.isoformat(),
                "completedAt": build_old_completed.isoformat(),
                "detailsUrl": None,
                "externalId": None,
            },
            {
                "id": "CR_BUILD_NEW",
                "name": "build",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "startedAt": build_new_started.isoformat(),
                "completedAt": build_new_completed.isoformat(),
                "detailsUrl": None,
                "externalId": None,
            },
            {
                "id": "CR_LINT_OLD",
                "name": "lint",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "startedAt": lint_started.isoformat(),
                "completedAt": lint_completed.isoformat(),
                "detailsUrl": None,
                "externalId": None,
            },
        ]

        sync_check_runs(self.pr, ctxs, head_sha)
        state = PRRevisionBuildState.objects.get(pull_request=self.pr)
        self.assertEqual(state.dirty_from_ts, lint_started)

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

    @override_settings(SYNCER_CI_SHA_STORAGE_DUAL_WRITE=False, SYNCER_CI_PR_STORAGE_WRITE=True)
    def test_sha_storage_dual_write_disabled(self) -> None:
        head_sha = "abc1234"
        sync_check_runs(
            self.pr,
            [
                {
                    "id": "CR_DUAL_OFF",
                    "name": "build",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                    "startedAt": "2025-10-20T00:10:00Z",
                    "completedAt": "2025-10-20T00:20:00Z",
                    "detailsUrl": None,
                    "externalId": None,
                }
            ],
            head_sha,
        )
        sync_status_contexts(
            self.pr,
            [
                {
                    "id": "SC_DUAL_OFF",
                    "context": "bors",
                    "state": "SUCCESS",
                    "targetUrl": None,
                    "description": "",
                    "createdAt": "2025-10-20T00:21:00Z",
                }
            ],
            head_sha,
        )

        self.assertEqual(CommitCheckRun.objects.count(), 0)
        self.assertEqual(CommitStatusContext.objects.count(), 0)

    @override_settings(SYNCER_CI_SHA_STORAGE_DUAL_WRITE=False, SYNCER_CI_PR_STORAGE_WRITE=False)
    def test_commit_storage_forced_when_pr_storage_disabled(self) -> None:
        head_sha = "abc1234"
        sync_check_runs(
            self.pr,
            [
                {
                    "id": "CR_PR_OFF",
                    "name": "build",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                    "startedAt": "2025-10-20T00:10:00Z",
                    "completedAt": "2025-10-20T00:20:00Z",
                    "detailsUrl": None,
                    "externalId": None,
                }
            ],
            head_sha,
        )
        sync_status_contexts(
            self.pr,
            [
                {
                    "id": "SC_PR_OFF",
                    "context": "bors",
                    "state": "SUCCESS",
                    "targetUrl": None,
                    "description": "",
                    "createdAt": "2025-10-20T00:21:00Z",
                }
            ],
            head_sha,
        )

        self.assertEqual(CheckRun.objects.count(), 0)
        self.assertEqual(StatusContext.objects.count(), 0)
        self.assertEqual(CommitCheckRun.objects.count(), 1)
        self.assertEqual(CommitStatusContext.objects.count(), 1)

    @override_settings(SYNCER_CI_SHA_STORAGE_DUAL_WRITE=True)
    def test_sha_storage_dual_write_writes_commit_scoped_rows(self) -> None:
        head_sha = "abc1234"
        before = timezone.now()
        sync_check_runs(
            self.pr,
            [
                {
                    "id": "CR_DUAL_ON",
                    "name": "build",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                    "startedAt": "2025-10-20T00:10:00Z",
                    "completedAt": "2025-10-20T00:20:00Z",
                    "detailsUrl": None,
                    "externalId": None,
                }
            ],
            head_sha,
        )
        sync_status_contexts(
            self.pr,
            [
                {
                    "id": "SC_DUAL_ON",
                    "context": "bors",
                    "state": "SUCCESS",
                    "targetUrl": None,
                    "description": "",
                    "createdAt": "2025-10-20T00:21:00Z",
                }
            ],
            head_sha,
        )

        ckr = CommitCheckRun.objects.get(github_node_id="CR_DUAL_ON")
        csc = CommitStatusContext.objects.get(github_node_id="SC_DUAL_ON")
        self.assertEqual(ckr.repository, self.repo)
        self.assertEqual(ckr.head_sha, head_sha)
        self.assertEqual(csc.repository, self.repo)
        self.assertEqual(csc.head_sha, head_sha)
        self.assertIsNotNone(ckr.last_synced_at)
        self.assertIsNotNone(csc.last_synced_at)
        self.assertGreaterEqual(ckr.last_synced_at, before)
        self.assertGreaterEqual(csc.last_synced_at, before)

    @override_settings(SYNCER_CI_SHA_STORAGE_DUAL_WRITE=True)
    def test_sha_storage_dual_write_checkrun_external_id_conflict_does_not_crash(self) -> None:
        head_sha = "abc1234"
        CommitCheckRun.objects.create(
            repository=self.repo,
            github_node_id="CR_EXISTING",
            head_sha=head_sha,
            name="Lint style",
            status="IN_PROGRESS",
            conclusion=None,
            external_id="ext-conflict",
            gh_started_at=timezone.now() - timezone.timedelta(minutes=5),
        )

        sync_check_runs(
            self.pr,
            [
                {
                    "id": "CR_NEW",
                    "name": "Lint style",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                    "startedAt": "2025-10-20T00:10:00Z",
                    "completedAt": "2025-10-20T00:20:00Z",
                    "detailsUrl": None,
                    "externalId": "ext-conflict",
                }
            ],
            head_sha,
        )

        row = CommitCheckRun.objects.get(external_id="ext-conflict")
        self.assertEqual(row.status, "COMPLETED")
        self.assertEqual(row.conclusion, "SUCCESS")
