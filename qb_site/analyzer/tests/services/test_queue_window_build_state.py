from __future__ import annotations

from django.test import TestCase
from django.utils import timezone

from analyzer.models import PRQueueWindowBuildState, PRRevisionBuildState, QueueRuleSet
from analyzer.services.queue_window_build_state import backfill_queue_window_build_states_for_repo
from core.models import Repository
from syncer.models import PullRequest


class TestQueueWindowBuildStateBackfillService(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="o", name="r", default_branch="master", is_active=True)
        self.rules = QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            is_active=True,
            require_open=True,
            require_not_draft=True,
            require_ci_success=False,
        )

    def _mk_pr(self, number: int, *, timeline_backfill_done: bool = True) -> PullRequest:
        now = timezone.now()
        return PullRequest.objects.create(
            repository=self.repo,
            number=number,
            author=None,
            state="open",
            is_draft=False,
            gh_created_at=now - timezone.timedelta(days=1),
            gh_updated_at=now,
            base_ref_name="master",
            head_ref_name="b",
            head_repo_owner_login="o",
            head_repo_name="r",
            title="t",
            body="",
            additions=0,
            deletions=0,
            changed_files_count=0,
            timeline_backfill_done=timeline_backfill_done,
            commits_backfill_done=timeline_backfill_done,
        )

    def test_dry_run_reports_changes_without_writing(self) -> None:
        pr = self._mk_pr(1)
        PRRevisionBuildState.objects.create(
            pull_request=pr,
            revision_version=2,
            windows_built_revision_version=2,
            windows_built_at=timezone.now(),
        )

        res = backfill_queue_window_build_states_for_repo(repository=self.repo, dry_run=True)

        self.assertEqual(res.prs_considered, 1)
        self.assertEqual(res.rows_created, 1)
        self.assertEqual(res.rows_updated, 0)
        self.assertEqual(PRQueueWindowBuildState.objects.count(), 0)

    def test_write_mode_creates_and_updates_rows(self) -> None:
        pr = self._mk_pr(2)
        built_at = timezone.now() - timezone.timedelta(days=1)
        PRRevisionBuildState.objects.create(
            pull_request=pr,
            revision_version=3,
            windows_built_revision_version=3,
            windows_built_at=built_at,
        )
        row = PRQueueWindowBuildState.objects.create(
            pull_request=pr,
            rule_set=self.rules,
            revision_version_built=1,
            windows_built_at=None,
            last_status="rebuilt",
            last_reason=None,
        )

        res = backfill_queue_window_build_states_for_repo(repository=self.repo, dry_run=False)

        self.assertEqual(res.prs_considered, 1)
        self.assertEqual(res.rows_created, 0)
        self.assertEqual(res.rows_updated, 1)
        row.refresh_from_db()
        self.assertEqual(row.revision_version_built, 3)
        self.assertEqual(row.windows_built_at, built_at)
        self.assertEqual(row.last_status, "backfilled")
        self.assertEqual(row.last_reason, "legacy_pr_build_state")

    def test_reports_progress_via_callback(self) -> None:
        pr1 = self._mk_pr(3)
        pr2 = self._mk_pr(4)
        for pr in (pr1, pr2):
            PRRevisionBuildState.objects.create(
                pull_request=pr,
                revision_version=1,
                windows_built_revision_version=1,
                windows_built_at=timezone.now(),
            )

        seen: list[tuple[int, int]] = []

        def _progress(processed: int, total: int) -> None:
            seen.append((processed, total))

        backfill_queue_window_build_states_for_repo(
            repository=self.repo,
            dry_run=True,
            progress_every=1,
            progress_cb=_progress,
        )
        self.assertEqual(seen[-1], (2, 2))
