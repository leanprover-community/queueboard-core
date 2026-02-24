from __future__ import annotations

from datetime import timedelta
import itertools
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import Repository
from syncer.models import CIShaFetchState, PullRequest, CheckRun, StatusContext
from syncer.tasks.sync_tasks import refresh_pending_ci_for_repo_task
from syncer.services.pr_sync_service import PRSyncService
from syncer.tests.factories import make_repo, make_pr


class TestRefreshPendingCITask(TestCase):
    def setUp(self) -> None:
        self.repo: Repository = make_repo()
        self.pr: PullRequest = make_pr(self.repo, 1)
        self._id_counter = itertools.count(1)

    def _make_checkrun(
        self,
        *,
        pr: PullRequest | None = None,
        node_id: str | None = None,
        status: str = "IN_PROGRESS",
        head_sha: str | None = "sha1",
        started_at_delta_hours: int = 1,
        last_synced_at_delta_hours: int | None = None,
    ) -> CheckRun:
        now = timezone.now()
        if pr is None:
            pr = self.pr
        if node_id is None:
            node_id = f"CR{next(self._id_counter)}"
        cr = CheckRun.objects.create(
            pull_request=pr,
            github_node_id=node_id,
            head_sha=head_sha,
            name="ci/test",
            status=status,
            conclusion=None,
            details_url=None,
            external_id=None,
            gh_started_at=now - timedelta(hours=started_at_delta_hours),
            gh_completed_at=None,
            last_synced_at=(
                now - timedelta(hours=last_synced_at_delta_hours) if last_synced_at_delta_hours is not None else None
            ),
        )
        return cr

    def _make_status(
        self,
        *,
        pr: PullRequest | None = None,
        node_id: str | None = None,
        state: str = "PENDING",
        head_sha: str | None = "sha1",
        created_delta_hours: int = 1,
        last_synced_at_delta_hours: int | None = None,
    ) -> StatusContext:
        now = timezone.now()
        if pr is None:
            pr = self.pr
        if node_id is None:
            node_id = f"SC{next(self._id_counter)}"
        sc = StatusContext.objects.create(
            pull_request=pr,
            github_node_id=node_id,
            rest_id=None,
            head_sha=head_sha,
            name="bors",
            state=state,
            target_url=None,
            description="",
            gh_created_at=now - timedelta(hours=created_delta_hours),
            last_synced_at=(
                now - timedelta(hours=last_synced_at_delta_hours) if last_synced_at_delta_hours is not None else None
            ),
        )
        return sc

    def test_skips_when_no_pending_ci(self) -> None:
        # No CheckRuns/StatusContexts at all and no head SHA: nothing to enqueue.
        self.pr.head_sha = None
        self.pr.save(update_fields=["head_sha", "updated_at"])
        res = refresh_pending_ci_for_repo_task(self.repo.id, max_prs=10, max_shas_per_pr=5, max_pending_hours=24)
        self.assertEqual(res.get("prs_enqueued"), 0)
        self.assertEqual(res.get("shas_enqueued"), 0)

    @mock.patch("syncer.tasks.sync_tasks.sync_ci_for_shas_task")
    def test_enqueues_when_head_sha_missing_contexts(self, mock_sync_ci_for_shas) -> None:
        # Head SHA is set but no contexts exist for it: enqueue CI-by-SHA refresh.
        self.pr.head_sha = "sha_missing"
        self.pr.save(update_fields=["head_sha", "updated_at"])
        mock_sync_ci_for_shas.delay.return_value.id = "task-1"

        res = refresh_pending_ci_for_repo_task(self.repo.id, max_prs=10, max_shas_per_pr=5, max_pending_hours=24)
        self.assertEqual(res.get("prs_enqueued"), 1)
        self.assertEqual(res.get("shas_enqueued"), 1)
        items = res.get("items") or []
        self.assertEqual(items[0]["number"], 1)
        self.assertEqual(items[0]["shas"], ["sha_missing"])
        mock_sync_ci_for_shas.delay.assert_called_once()

    @override_settings(SYNCER_CI_SHA_SETTLE_WINDOW_SECONDS=60)
    @mock.patch("syncer.tasks.sync_tasks.sync_ci_for_shas_task")
    def test_skips_when_head_sha_backoff_not_found(self, mock_sync_ci_for_shas) -> None:
        self.pr.head_sha = "sha_missing"
        self.pr.save(update_fields=["head_sha", "updated_at"])
        state = CIShaFetchState.objects.create(
            repository=self.repo,
            sha="sha_missing",
            last_attempted_at=timezone.now(),
            last_success_at=None,
            last_result="not_found",
            attempts=2,
        )
        CIShaFetchState.objects.filter(pk=state.pk).update(created_at=timezone.now() - timedelta(seconds=120))
        res = refresh_pending_ci_for_repo_task(self.repo.id, max_prs=10, max_shas_per_pr=5, max_pending_hours=24)
        self.assertEqual(res.get("prs_enqueued"), 0)
        self.assertEqual(res.get("shas_enqueued"), 0)
        self.assertEqual(res.get("prs_skipped_backoff"), 1)
        self.assertEqual(res.get("shas_skipped_backoff"), 1)
        mock_sync_ci_for_shas.delay.assert_not_called()

    @override_settings(SYNCER_CI_SHA_SETTLE_WINDOW_SECONDS=60)
    @mock.patch("syncer.tasks.sync_tasks.sync_ci_for_shas_task")
    def test_skips_when_head_sha_backoff_filtered(self, mock_sync_ci_for_shas) -> None:
        self.pr.head_sha = "sha_filtered"
        self.pr.save(update_fields=["head_sha", "updated_at"])
        state = CIShaFetchState.objects.create(
            repository=self.repo,
            sha="sha_filtered",
            last_attempted_at=timezone.now(),
            last_success_at=None,
            last_result="filtered",
            attempts=2,
        )
        CIShaFetchState.objects.filter(pk=state.pk).update(created_at=timezone.now() - timedelta(seconds=120))
        res = refresh_pending_ci_for_repo_task(self.repo.id, max_prs=10, max_shas_per_pr=5, max_pending_hours=24)
        self.assertEqual(res.get("prs_enqueued"), 0)
        self.assertEqual(res.get("shas_enqueued"), 0)
        self.assertEqual(res.get("prs_skipped_backoff"), 1)
        self.assertEqual(res.get("shas_skipped_backoff"), 1)
        mock_sync_ci_for_shas.delay.assert_not_called()

    @override_settings(SYNCER_CI_SHA_SETTLE_WINDOW_SECONDS=3600)
    @mock.patch("syncer.tasks.sync_tasks.sync_ci_for_shas_task")
    def test_enqueues_when_head_sha_backoff_not_found_within_settle_window(self, mock_sync_ci_for_shas) -> None:
        self.pr.head_sha = "sha_missing"
        self.pr.save(update_fields=["head_sha", "updated_at"])
        state = CIShaFetchState.objects.create(
            repository=self.repo,
            sha="sha_missing",
            last_attempted_at=timezone.now(),
            last_success_at=None,
            last_result="not_found",
            attempts=1,
        )
        CIShaFetchState.objects.filter(pk=state.pk).update(created_at=timezone.now() - timedelta(minutes=5))
        mock_sync_ci_for_shas.delay.return_value.id = "task-1"
        res = refresh_pending_ci_for_repo_task(self.repo.id, max_prs=10, max_shas_per_pr=5, max_pending_hours=24)
        self.assertEqual(res.get("prs_enqueued"), 1)
        self.assertEqual(res.get("shas_enqueued"), 1)
        items = res.get("items") or []
        self.assertEqual(items[0]["shas"], ["sha_missing"])
        mock_sync_ci_for_shas.delay.assert_called_once()

    @override_settings(SYNCER_CI_SHA_SETTLE_WINDOW_SECONDS=3600, SYNCER_CI_SHA_HARD_CAP_DAYS=400)
    @mock.patch("syncer.tasks.sync_tasks.sync_ci_for_shas_task")
    def test_skips_when_head_sha_backoff_past_hard_cap(self, mock_sync_ci_for_shas) -> None:
        self.pr.head_sha = "sha_filtered"
        self.pr.gh_updated_at = timezone.now() - timedelta(days=401)
        self.pr.save(update_fields=["head_sha", "gh_updated_at", "updated_at"])
        state = CIShaFetchState.objects.create(
            repository=self.repo,
            sha="sha_filtered",
            last_attempted_at=timezone.now(),
            last_success_at=None,
            last_result="filtered",
            attempts=2,
        )
        CIShaFetchState.objects.filter(pk=state.pk).update(created_at=timezone.now() - timedelta(minutes=5))
        res = refresh_pending_ci_for_repo_task(self.repo.id, max_prs=10, max_shas_per_pr=5, max_pending_hours=24)
        self.assertEqual(res.get("prs_enqueued"), 0)
        self.assertEqual(res.get("shas_enqueued"), 0)
        self.assertEqual(res.get("prs_skipped_backoff"), 1)
        self.assertEqual(res.get("shas_skipped_backoff"), 1)
        mock_sync_ci_for_shas.delay.assert_not_called()

    def test_skips_when_head_sha_has_contexts(self) -> None:
        # Completed CI exists for head SHA; no pending -> no enqueue.
        self._make_checkrun(status="COMPLETED", head_sha=self.pr.head_sha, started_at_delta_hours=1)
        res = refresh_pending_ci_for_repo_task(self.repo.id, max_prs=10, max_shas_per_pr=5, max_pending_hours=24)
        self.assertEqual(res.get("prs_enqueued"), 0)
        self.assertEqual(res.get("shas_enqueued"), 0)

    def test_ignores_stale_pending_status_context_when_newer_success_exists(self) -> None:
        # Older pending status should not trigger refresh if a newer success exists for same name+SHA.
        self._make_status(state="PENDING", head_sha=self.pr.head_sha, created_delta_hours=5)
        self._make_status(state="SUCCESS", head_sha=self.pr.head_sha, created_delta_hours=1)
        res = refresh_pending_ci_for_repo_task(self.repo.id, max_prs=10, max_shas_per_pr=5, max_pending_hours=24)
        self.assertEqual(res.get("prs_enqueued"), 0)
        self.assertEqual(res.get("shas_enqueued"), 0)

    @mock.patch("syncer.tasks.sync_tasks.sync_ci_for_shas_task")
    def test_ignores_stale_pending_checkrun_when_newer_completed_exists(self, mock_sync_ci_for_shas) -> None:
        # Older queued check run should not trigger refresh if a newer completed run exists for same name+SHA.
        self._make_checkrun(status="QUEUED", head_sha=self.pr.head_sha, started_at_delta_hours=5)
        self._make_checkrun(status="COMPLETED", head_sha=self.pr.head_sha, started_at_delta_hours=1)
        res = refresh_pending_ci_for_repo_task(self.repo.id, max_prs=10, max_shas_per_pr=5, max_pending_hours=24)
        self.assertEqual(res.get("prs_enqueued"), 0)
        self.assertEqual(res.get("shas_enqueued"), 0)
        mock_sync_ci_for_shas.delay.assert_not_called()

    @mock.patch("syncer.tasks.sync_tasks.sync_ci_for_shas_task")
    def test_skips_missing_head_ci_when_unavailable(self, mock_sync_ci_for_shas) -> None:
        # Missing head contexts should be ignored when head_ci_state is UNAVAILABLE.
        self.pr.head_sha = "sha_missing"
        self.pr.head_ci_state = "UNAVAILABLE"
        self.pr.save(update_fields=["head_sha", "head_ci_state", "updated_at"])

        res = refresh_pending_ci_for_repo_task(self.repo.id, max_prs=10, max_shas_per_pr=5, max_pending_hours=24)
        self.assertEqual(res.get("prs_enqueued"), 0)
        self.assertEqual(res.get("shas_enqueued"), 0)
        mock_sync_ci_for_shas.delay.assert_not_called()

    @mock.patch("syncer.tasks.sync_tasks.sync_ci_for_shas_task")
    def test_enqueues_when_head_sha_differs_from_existing_contexts(self, mock_sync_ci_for_shas) -> None:
        # Existing CI contexts are for a different head SHA; current head is missing.
        self._make_checkrun(status="COMPLETED", head_sha="old_sha", started_at_delta_hours=1)
        self.pr.head_sha = "new_sha"
        self.pr.save(update_fields=["head_sha", "updated_at"])
        mock_sync_ci_for_shas.delay.return_value.id = "task-2"

        res = refresh_pending_ci_for_repo_task(self.repo.id, max_prs=10, max_shas_per_pr=5, max_pending_hours=24)
        self.assertEqual(res.get("prs_enqueued"), 1)
        self.assertEqual(res.get("shas_enqueued"), 1)
        items = res.get("items") or []
        self.assertEqual(items[0]["shas"], ["new_sha"])
        mock_sync_ci_for_shas.delay.assert_called_once()

    @mock.patch("syncer.tasks.sync_tasks.sync_ci_for_shas_task")
    def test_open_prs_prioritized_over_closed(self, mock_sync_ci_for_shas) -> None:
        now = timezone.now()
        make_pr(self.repo, 20, state="closed", gh_updated_at=now - timedelta(hours=2), head_sha="sha_closed")
        open_pr = make_pr(self.repo, 21, state="open", gh_updated_at=now - timedelta(hours=1), head_sha="sha_open")
        self._make_checkrun(pr=open_pr, status="IN_PROGRESS", head_sha="sha_open", started_at_delta_hours=1)

        mock_sync_ci_for_shas.delay.return_value.id = "task-4"
        res = refresh_pending_ci_for_repo_task(self.repo.id, max_prs=1, max_shas_per_pr=5, max_pending_hours=24)
        self.assertEqual(res.get("prs_enqueued"), 1)
        items = res.get("items") or []
        self.assertEqual(items[0]["number"], 21)
        mock_sync_ci_for_shas.delay.assert_called_once()

    @mock.patch("syncer.tasks.sync_tasks.sync_ci_for_shas_task")
    def test_enqueues_for_recent_pending_ci(self, mock_sync_ci_for_shas) -> None:
        # One PR with a pending CheckRun that has never been explicitly synced.
        self.pr.head_sha = "shaA"
        self.pr.save(update_fields=["head_sha", "updated_at"])
        self._make_checkrun(status="IN_PROGRESS", head_sha="shaA", started_at_delta_hours=1, last_synced_at_delta_hours=None)

        # Avoid hitting the real broker; run task in-process and assert we request CI for the expected SHA.
        mock_sync_ci_for_shas.delay.return_value.id = "task-1"

        res = refresh_pending_ci_for_repo_task(self.repo.id, max_prs=10, max_shas_per_pr=5, max_pending_hours=24)
        self.assertEqual(res.get("prs_enqueued"), 1)
        self.assertEqual(res.get("shas_enqueued"), 1)
        items = res.get("items") or []
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["number"], 1)
        self.assertEqual(items[0]["shas"], ["shaA"])
        self.assertTrue(items[0]["task_id"])
        mock_sync_ci_for_shas.delay.assert_called_once()

    @mock.patch("syncer.tasks.sync_tasks.sync_ci_for_shas_task")
    def test_timeout_excludes_old_pending_ci(self, mock_sync_ci_for_shas) -> None:
        # Pending CheckRun whose pending_duration exceeds max_pending_hours should be skipped.
        # Origin is taken from gh_started_at; last_synced_at is far enough after origin.
        self.pr.head_sha = "shaB"
        self.pr.save(update_fields=["head_sha", "updated_at"])
        self._make_checkrun(
            status="IN_PROGRESS",
            head_sha="shaB",
            started_at_delta_hours=48,
            last_synced_at_delta_hours=24,
        )

        res = refresh_pending_ci_for_repo_task(self.repo.id, max_prs=10, max_shas_per_pr=5, max_pending_hours=12)
        self.assertEqual(res.get("prs_enqueued"), 0)
        self.assertEqual(res.get("shas_enqueued"), 0)
        mock_sync_ci_for_shas.delay.assert_not_called()

    @mock.patch("syncer.tasks.sync_tasks.sync_ci_for_shas_task")
    def test_refresh_after_pr_sync_pending(self, mock_sync_ci_for_shas) -> None:
        self.pr.head_sha = None
        self.pr.save(update_fields=["head_sha", "updated_at"])
        svc = PRSyncService()
        now = timezone.now()
        recent = (now - timedelta(minutes=5)).isoformat()
        started = (now - timedelta(minutes=2)).isoformat()
        bundle = {
            "number": 5,
            "state": "OPEN",
            "isDraft": False,
            "title": "Pending CI",
            "body": "",
            "createdAt": recent,
            "updatedAt": recent,
            "baseRefName": "master",
            "headRefName": "b",
            "headRefOid": "sha_pending",
            "headRepositoryOwner": {"login": "o"},
            "headRepository": {"name": "r"},
            "additions": 0,
            "deletions": 0,
            "changedFiles": 0,
            "author": {"login": "alice"},
            "labels": {"nodes": []},
            "timelineItems": {"nodes": []},
            "commits": {
                "nodes": [
                    {
                        "commit": {
                            "oid": "sha_pending",
                            "statusCheckRollup": {
                                "contexts": {
                                    "nodes": [
                                        {
                                            "__typename": "CheckRun",
                                            "id": "CR_pending",
                                            "name": "build",
                                            "status": "IN_PROGRESS",
                                            "conclusion": None,
                                            "startedAt": started,
                                            "completedAt": None,
                                        }
                                    ]
                                }
                            },
                        }
                    }
                ]
            },
        }
        svc.sync_pull_request_bundle(self.repo, bundle, dry_run=False)

        mock_sync_ci_for_shas.delay.return_value.id = "task-2"
        res = refresh_pending_ci_for_repo_task(self.repo.id, max_prs=10, max_shas_per_pr=5, max_pending_hours=24)
        self.assertEqual(res.get("prs_enqueued"), 1)
        self.assertEqual(res.get("shas_enqueued"), 1)
        items = res.get("items") or []
        self.assertEqual(items[0]["number"], 5)
        self.assertEqual(items[0]["shas"], ["sha_pending"])
        mock_sync_ci_for_shas.delay.assert_called_once()

    @mock.patch("syncer.tasks.sync_tasks.sync_ci_for_shas_task")
    def test_scans_past_ineligible_prs_to_find_actionable(self, mock_sync_ci_for_shas) -> None:
        now = timezone.now()
        pr1 = make_pr(self.repo, 10, gh_updated_at=now - timedelta(hours=3), head_sha="sha_old")
        pr2 = make_pr(self.repo, 11, gh_updated_at=now - timedelta(hours=2), head_sha="sha_old2")
        pr3 = make_pr(self.repo, 12, gh_updated_at=now - timedelta(hours=1), head_sha="sha_ok")

        # pr1: stale pending (too old)
        self._make_checkrun(
            pr=pr1,
            status="IN_PROGRESS",
            head_sha="sha_old",
            started_at_delta_hours=48,
            last_synced_at_delta_hours=0,
        )
        # pr2: recent pending but too old by last_synced_at relative to origin
        self._make_status(
            pr=pr2,
            state="PENDING",
            head_sha="sha_old2",
            created_delta_hours=48,
            last_synced_at_delta_hours=0,
        )
        # pr3: actionable pending
        self._make_status(pr=pr3, state="PENDING", head_sha="sha_ok", created_delta_hours=1)

        mock_sync_ci_for_shas.delay.return_value.id = "task-3"
        res = refresh_pending_ci_for_repo_task(self.repo.id, max_prs=1, max_shas_per_pr=5, max_pending_hours=12)

        self.assertEqual(res.get("prs_enqueued"), 1)
        self.assertEqual(res.get("shas_enqueued"), 1)
        self.assertEqual(res.get("prs_skipped_stale"), 2)
        items = res.get("items") or []
        self.assertEqual(items[0]["number"], 12)
        self.assertEqual(items[0]["shas"], ["sha_ok"])
        mock_sync_ci_for_shas.delay.assert_called_once()

    @override_settings(SYNCER_CI_SHA_SETTLE_WINDOW_SECONDS=60)
    @mock.patch("syncer.tasks.sync_tasks.sync_ci_for_shas_task")
    def test_multi_sha_gating_keeps_allowed_sha_when_head_sha_blocked(self, mock_sync_ci_for_shas) -> None:
        self.pr.head_sha = "sha_missing"
        self.pr.save(update_fields=["head_sha", "updated_at"])
        self._make_status(state="PENDING", head_sha="sha_ok", created_delta_hours=1, last_synced_at_delta_hours=None)

        blocked = CIShaFetchState.objects.create(
            repository=self.repo,
            sha="sha_missing",
            last_attempted_at=timezone.now(),
            last_success_at=None,
            last_result="filtered",
            attempts=2,
        )
        CIShaFetchState.objects.filter(pk=blocked.pk).update(created_at=timezone.now() - timedelta(minutes=5))
        mock_sync_ci_for_shas.delay.return_value.id = "task-mixed"

        res = refresh_pending_ci_for_repo_task(self.repo.id, max_prs=10, max_shas_per_pr=5, max_pending_hours=24)
        self.assertEqual(res.get("prs_enqueued"), 1)
        self.assertEqual(res.get("shas_enqueued"), 1)
        self.assertEqual(res.get("shas_skipped_backoff"), 1)
        items = res.get("items") or []
        self.assertEqual(items[0]["reason"], "missing_head_ci")
        self.assertEqual(items[0]["shas"], ["sha_ok"])
        mock_sync_ci_for_shas.delay.assert_called_once()

    @mock.patch("syncer.tasks.sync_tasks.sync_ci_for_shas_task")
    def test_mixed_missing_head_and_pending_reasons(self, mock_sync_ci_for_shas) -> None:
        now = timezone.now()
        make_pr(self.repo, 30, state="open", gh_updated_at=now - timedelta(hours=2), head_sha="sha_missing")
        pr_pending = make_pr(self.repo, 31, state="open", gh_updated_at=now - timedelta(hours=1), head_sha="sha_pending")
        self._make_status(pr=pr_pending, state="PENDING", head_sha="sha_pending", created_delta_hours=1)

        mock_sync_ci_for_shas.delay.side_effect = [
            mock.Mock(id="task-a"),
            mock.Mock(id="task-b"),
        ]
        res = refresh_pending_ci_for_repo_task(self.repo.id, max_prs=2, max_shas_per_pr=5, max_pending_hours=24)
        self.assertEqual(res.get("prs_enqueued"), 2)
        reasons = {item["reason"] for item in (res.get("items") or [])}
        self.assertEqual(reasons, {"missing_head_ci", "pending_ci"})
        mock_sync_ci_for_shas.delay.assert_called()

    @override_settings(SYNCER_CI_SHA_SETTLE_WINDOW_SECONDS=60)
    @mock.patch("syncer.tasks.sync_tasks.sync_ci_for_shas_task")
    def test_scans_past_backoff_blocked_prs_to_enqueue_later_pr(self, mock_sync_ci_for_shas) -> None:
        now = timezone.now()
        pr1 = make_pr(self.repo, 40, state="open", gh_updated_at=now - timedelta(hours=3), head_sha="sha_block_1")
        pr2 = make_pr(self.repo, 41, state="open", gh_updated_at=now - timedelta(hours=2), head_sha="sha_block_2")
        make_pr(self.repo, 42, state="open", gh_updated_at=now - timedelta(hours=1), head_sha="sha_ok")
        for pr, sha in ((pr1, "sha_block_1"), (pr2, "sha_block_2")):
            state = CIShaFetchState.objects.create(
                repository=self.repo,
                sha=sha,
                last_attempted_at=timezone.now(),
                last_success_at=None,
                last_result="filtered",
                attempts=2,
            )
            CIShaFetchState.objects.filter(pk=state.pk).update(created_at=timezone.now() - timedelta(minutes=5))

        mock_sync_ci_for_shas.delay.return_value.id = "task-later"
        res = refresh_pending_ci_for_repo_task(self.repo.id, max_prs=1, max_shas_per_pr=5, max_pending_hours=24)
        self.assertEqual(res.get("prs_enqueued"), 1)
        self.assertEqual(res.get("prs_skipped_backoff"), 2)
        items = res.get("items") or []
        self.assertEqual(items[0]["number"], 42)
        mock_sync_ci_for_shas.delay.assert_called_once()

    @mock.patch("syncer.tasks.sync_tasks.sync_ci_for_shas_task")
    def test_reports_scan_counters(self, mock_sync_ci_for_shas) -> None:
        now = timezone.now()
        pr1 = make_pr(self.repo, 50, state="open", gh_updated_at=now - timedelta(hours=2), head_sha="sha_old")
        pr2 = make_pr(self.repo, 51, state="open", gh_updated_at=now - timedelta(hours=1), head_sha="sha_ok")
        self._make_checkrun(
            pr=pr1,
            status="IN_PROGRESS",
            head_sha="sha_old",
            started_at_delta_hours=48,
            last_synced_at_delta_hours=0,
        )
        self._make_status(pr=pr2, state="PENDING", head_sha="sha_ok", created_delta_hours=1)

        mock_sync_ci_for_shas.delay.return_value.id = "task-counters"
        res = refresh_pending_ci_for_repo_task(self.repo.id, max_prs=1, max_shas_per_pr=5, max_pending_hours=12)

        self.assertEqual(res.get("prs_considered"), 1)
        self.assertEqual(res.get("backlog_prs_actionable_scanned"), 1)
        self.assertEqual(res.get("prs_scanned_total"), 2)
        self.assertEqual(res.get("prs_seen_pending_or_missing_head"), 2)
        self.assertEqual(res.get("prs_skipped_stale"), 1)
        self.assertEqual(res.get("prs_enqueued"), 1)
