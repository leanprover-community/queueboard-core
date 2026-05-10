from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from syncer.models import CommitCheckRun, CommitStatusContext
from syncer.services.sub.ci_sync import _bump_latest_ci_synced_at, sync_check_runs, sync_status_contexts
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
        self.assertEqual(CommitCheckRun.objects.filter(repository=self.repo, head_sha=head_sha).count(), 1)
        self.assertEqual(res.created, 1)
        cr = CommitCheckRun.objects.get(repository=self.repo, github_node_id="CR1")
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
        self.assertEqual(CommitStatusContext.objects.filter(repository=self.repo, head_sha=head_sha).count(), 1)
        self.assertEqual(res.created, 1)
        sc = CommitStatusContext.objects.get(repository=self.repo, github_node_id="SC1")
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

    def test_commit_storage_writes_commit_scoped_rows(self) -> None:
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

        self.assertEqual(CommitCheckRun.objects.count(), 1)
        self.assertEqual(CommitStatusContext.objects.count(), 1)

    def test_commit_storage_sets_last_synced_at(self) -> None:
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

    def test_in_progress_cancelled_coerced_to_completed(self) -> None:
        """GitHub can deliver IN_PROGRESS+CANCELLED during a race; we must store COMPLETED."""
        head_sha = "abc1234"
        ctxs = [
            {
                "id": "CR_CANCEL",
                "name": "build",
                "status": "IN_PROGRESS",
                "conclusion": "CANCELLED",
                "startedAt": "2025-10-20T00:00:00Z",
                "completedAt": None,
                "detailsUrl": None,
                "externalId": None,
            }
        ]
        sync_check_runs(self.pr, ctxs, head_sha)
        cr = CommitCheckRun.objects.get(repository=self.repo, github_node_id="CR_CANCEL")
        self.assertEqual(cr.status, "COMPLETED")
        self.assertEqual(cr.conclusion, "CANCELLED")

    def test_queued_with_conclusion_coerced_to_completed(self) -> None:
        """Any non-null conclusion coerces status to COMPLETED regardless of original status."""
        head_sha = "abc1234"
        ctxs = [
            {
                "id": "CR_QUEUED_FAIL",
                "name": "build",
                "status": "QUEUED",
                "conclusion": "FAILURE",
                "startedAt": None,
                "completedAt": "2025-10-20T01:00:00Z",
                "detailsUrl": None,
                "externalId": None,
            }
        ]
        sync_check_runs(self.pr, ctxs, head_sha)
        cr = CommitCheckRun.objects.get(repository=self.repo, github_node_id="CR_QUEUED_FAIL")
        self.assertEqual(cr.status, "COMPLETED")
        self.assertEqual(cr.conclusion, "FAILURE")

    def test_existing_in_progress_row_updated_to_completed_on_cancelled_refresh(self) -> None:
        """An existing IN_PROGRESS row is updated to COMPLETED when a refresh delivers CANCELLED."""
        head_sha = "abc1234"
        # Initial write: genuinely in progress
        ctxs = [
            {
                "id": "CR_LIVE",
                "name": "build",
                "status": "IN_PROGRESS",
                "conclusion": None,
                "startedAt": "2025-10-20T00:00:00Z",
                "completedAt": None,
                "detailsUrl": None,
                "externalId": None,
            }
        ]
        sync_check_runs(self.pr, ctxs, head_sha)
        cr = CommitCheckRun.objects.get(repository=self.repo, github_node_id="CR_LIVE")
        self.assertEqual(cr.status, "IN_PROGRESS")
        # Refresh: GitHub now returns IN_PROGRESS+CANCELLED (race condition)
        ctxs[0]["conclusion"] = "CANCELLED"
        res = sync_check_runs(self.pr, ctxs, head_sha)
        self.assertEqual(res.updated, 1)
        cr.refresh_from_db()
        self.assertEqual(cr.status, "COMPLETED")
        self.assertEqual(cr.conclusion, "CANCELLED")

    def test_completed_status_unchanged_when_conclusion_present(self) -> None:
        """COMPLETED+SUCCESS is written as-is; coercion is a no-op for already-COMPLETED rows."""
        head_sha = "abc1234"
        ctxs = [
            {
                "id": "CR_OK",
                "name": "build",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "startedAt": "2025-10-20T00:00:00Z",
                "completedAt": "2025-10-20T01:00:00Z",
                "detailsUrl": None,
                "externalId": None,
            }
        ]
        sync_check_runs(self.pr, ctxs, head_sha)
        cr = CommitCheckRun.objects.get(repository=self.repo, github_node_id="CR_OK")
        self.assertEqual(cr.status, "COMPLETED")
        self.assertEqual(cr.conclusion, "SUCCESS")

    def test_in_progress_without_conclusion_stored_as_is(self) -> None:
        """Genuinely in-progress runs (null conclusion) are stored without coercion."""
        head_sha = "abc1234"
        ctxs = [
            {
                "id": "CR_RUNNING",
                "name": "build",
                "status": "IN_PROGRESS",
                "conclusion": None,
                "startedAt": "2025-10-20T00:00:00Z",
                "completedAt": None,
                "detailsUrl": None,
                "externalId": None,
            }
        ]
        sync_check_runs(self.pr, ctxs, head_sha)
        cr = CommitCheckRun.objects.get(repository=self.repo, github_node_id="CR_RUNNING")
        self.assertEqual(cr.status, "IN_PROGRESS")
        self.assertIsNone(cr.conclusion)

    def test_checkrun_external_id_conflict_does_not_crash(self) -> None:
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


class TestLatestCISyncedAtWatermark(TestCase):
    """Tests for the doc 045 latest_ci_synced_at watermark.

    The watermark drives the queue-window sweep's CI-staleness predicate,
    so it must obey two invariants:

    1. Monotone: writers advance it forward, never reset it.
    2. No-op = no-write: a CI sub-sync that produced no content change
       (created=0, updated=0) does not advance the watermark, and the
       bump helper itself does not write the row when ``now <= existing``.

    The second invariant is load-bearing for avoiding the rebuild-loop
    class of bug fixed in 73d0446 / 088434e / 78c29cc.  Reviewers should
    not "simplify" by making the bump unconditional.
    """

    def setUp(self) -> None:
        self.repo = make_repo()
        self.pr = make_pr(self.repo, 1)

    # ---- _bump_latest_ci_synced_at unit tests ----

    def test_bump_first_call_sets_column(self) -> None:
        now = timezone.now()
        _bump_latest_ci_synced_at(self.pr, now)
        state = PRRevisionBuildState.objects.get(pull_request=self.pr)
        self.assertEqual(state.latest_ci_synced_at, now)

    def test_bump_later_now_advances(self) -> None:
        t1 = timezone.now()
        _bump_latest_ci_synced_at(self.pr, t1)
        t2 = t1 + timedelta(minutes=5)
        _bump_latest_ci_synced_at(self.pr, t2)
        state = PRRevisionBuildState.objects.get(pull_request=self.pr)
        self.assertEqual(state.latest_ci_synced_at, t2)

    def test_bump_earlier_now_is_noop(self) -> None:
        t2 = timezone.now()
        _bump_latest_ci_synced_at(self.pr, t2)
        state = PRRevisionBuildState.objects.get(pull_request=self.pr)
        baseline_updated_at = state.updated_at

        # Earlier ``now`` must not advance the watermark *and* must not
        # touch the row at all (updated_at unchanged) — pinning the
        # no-op-no-write invariant against a future refactor that uses
        # Greatest() unconditionally.
        t1 = t2 - timedelta(minutes=5)
        _bump_latest_ci_synced_at(self.pr, t1)
        state.refresh_from_db()
        self.assertEqual(state.latest_ci_synced_at, t2)
        self.assertEqual(state.updated_at, baseline_updated_at)

    def test_bump_equal_now_is_noop(self) -> None:
        t = timezone.now()
        _bump_latest_ci_synced_at(self.pr, t)
        state = PRRevisionBuildState.objects.get(pull_request=self.pr)
        baseline_updated_at = state.updated_at

        # Same ``now`` is also a no-op (the WHERE uses strict ``__lt``).
        _bump_latest_ci_synced_at(self.pr, t)
        state.refresh_from_db()
        self.assertEqual(state.latest_ci_synced_at, t)
        self.assertEqual(state.updated_at, baseline_updated_at)

    def test_bump_get_or_creates_state(self) -> None:
        # No PRRevisionBuildState row exists yet for this PR.
        self.assertFalse(PRRevisionBuildState.objects.filter(pull_request=self.pr).exists())
        now = timezone.now()
        _bump_latest_ci_synced_at(self.pr, now)
        state = PRRevisionBuildState.objects.get(pull_request=self.pr)
        self.assertEqual(state.latest_ci_synced_at, now)

    # ---- Sub-sync integration tests ----

    def _checkrun_ctx(self, gid: str = "CR1") -> dict:
        return {
            "id": gid,
            "name": "build",
            "status": "IN_PROGRESS",
            "conclusion": None,
            "startedAt": "2025-10-20T00:00:00Z",
            "completedAt": None,
            "detailsUrl": None,
            "externalId": None,
        }

    def _status_ctx(self, gid: str = "SC1") -> dict:
        return {
            "id": gid,
            "context": "ci/circleci",
            "state": "PENDING",
            "targetUrl": None,
            "description": None,
            "createdAt": "2025-10-20T00:00:00Z",
        }

    def test_sync_check_runs_advances_watermark_on_create(self) -> None:
        before = timezone.now()
        sync_check_runs(self.pr, [self._checkrun_ctx()], "abc1234")
        state = PRRevisionBuildState.objects.get(pull_request=self.pr)
        self.assertIsNotNone(state.latest_ci_synced_at)
        self.assertGreaterEqual(state.latest_ci_synced_at, before)

    def test_sync_check_runs_advances_watermark_on_update(self) -> None:
        sync_check_runs(self.pr, [self._checkrun_ctx()], "abc1234")
        state = PRRevisionBuildState.objects.get(pull_request=self.pr)
        first_watermark = state.latest_ci_synced_at

        # Status flip: PENDING → COMPLETED counts as an update.
        ctx = self._checkrun_ctx()
        ctx["status"] = "COMPLETED"
        ctx["conclusion"] = "SUCCESS"
        ctx["completedAt"] = "2025-10-20T01:00:00Z"
        sync_check_runs(self.pr, [ctx], "abc1234")
        state.refresh_from_db()
        self.assertIsNotNone(state.latest_ci_synced_at)
        self.assertGreater(state.latest_ci_synced_at, first_watermark)

    def test_sync_check_runs_noop_does_not_advance_watermark(self) -> None:
        """Identical re-sync: created=0, updated=0 → watermark unchanged.

        This is the test the design ultimately stands or falls on (mirrors
        73d0446's no-churn test).  If this ever fails, the active-PR
        rebuild loop the design was meant to avoid is back.
        """
        sync_check_runs(self.pr, [self._checkrun_ctx()], "abc1234")
        state = PRRevisionBuildState.objects.get(pull_request=self.pr)
        first_watermark = state.latest_ci_synced_at
        first_updated_at = state.updated_at

        # Re-sync identical payload — must produce zero CI content writes.
        result = sync_check_runs(self.pr, [self._checkrun_ctx()], "abc1234")
        self.assertEqual(result.created, 0)
        self.assertEqual(result.updated, 0)
        state.refresh_from_db()
        self.assertEqual(state.latest_ci_synced_at, first_watermark)
        self.assertEqual(state.updated_at, first_updated_at)

    def test_sync_check_runs_empty_contexts_does_not_create_state(self) -> None:
        """Empty contexts list: created=0, updated=0 → no watermark write,
        and crucially no PRRevisionBuildState row should be created.
        The bump helper short-circuits before its get_or_create."""
        result = sync_check_runs(self.pr, [], "abc1234")
        self.assertEqual(result.created, 0)
        self.assertEqual(result.updated, 0)
        self.assertFalse(PRRevisionBuildState.objects.filter(pull_request=self.pr).exists())

    def test_sync_status_contexts_advances_watermark_on_create(self) -> None:
        before = timezone.now()
        sync_status_contexts(self.pr, [self._status_ctx()], "abc1234")
        state = PRRevisionBuildState.objects.get(pull_request=self.pr)
        self.assertIsNotNone(state.latest_ci_synced_at)
        self.assertGreaterEqual(state.latest_ci_synced_at, before)

    def test_sync_status_contexts_noop_does_not_advance_watermark(self) -> None:
        sync_status_contexts(self.pr, [self._status_ctx()], "abc1234")
        state = PRRevisionBuildState.objects.get(pull_request=self.pr)
        first_watermark = state.latest_ci_synced_at
        first_updated_at = state.updated_at

        result = sync_status_contexts(self.pr, [self._status_ctx()], "abc1234")
        self.assertEqual(result.created, 0)
        self.assertEqual(result.updated, 0)
        state.refresh_from_db()
        self.assertEqual(state.latest_ci_synced_at, first_watermark)
        self.assertEqual(state.updated_at, first_updated_at)

    def test_sync_status_contexts_empty_contexts_does_not_create_state(self) -> None:
        result = sync_status_contexts(self.pr, [], "abc1234")
        self.assertEqual(result.created, 0)
        self.assertEqual(result.updated, 0)
        self.assertFalse(PRRevisionBuildState.objects.filter(pull_request=self.pr).exists())
